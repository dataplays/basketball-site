#!/usr/bin/env python3
"""
edges_dashboard.py — "Today's Edges" cross-league aggregator (/edges).
======================================================================
One page that ranks EVERY game across all of our live-projection boards by the
gap between the model's projection and the market's consensus line — so you find
the edge in one glance instead of clicking through six league tabs.

How it works
------------
* For each in-season league it calls that board's own `fetch_and_project()`
  (the exact projections the /nba, /wnba, /cbb, /wcbb pages show), taking the
  live + upcoming games.
* It pulls that league's consensus **spread** and **total** from The Odds API
  via the shared `market_lines_multi` engine (one cheap bulk call per league),
  matches each game by team name, and computes the edge:
      spread edge = proj_spread + market_home_line   (+ => back HOME)
      total  edge = proj_total  - market_total        (+ => OVER)
* Every matched game becomes a row; rows are ranked by their strongest edge and
  shown newest-market-first, with green chips for edges >= STRONG_EDGE points.

Season-aware for free: a league with no games (off-season) contributes nothing,
so the page naturally shows only what is actually playing.

The assembled slate is cached ~40s (each board caches its own scoreboard, and
the market engine caches per league ~90s), so the 60s auto-refresh is cheap.

Standalone:
    py -3 edges_dashboard.py                  # http://localhost:5015
    py -3 edges_dashboard.py --once           # print the ranked edges, no server
    py -3 edges_dashboard.py --date 2026-07-08 --port 8080

Also mounted at /edges on the basketball-site (module-level `app`, relative
fetch URLs so it works under the mount).
"""

from __future__ import annotations

import argparse
import importlib
import threading
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = None

from flask import Flask, jsonify

import market_lines_multi as ml

# ── League registry ───────────────────────────────────────────────────────────
# prefix -> (display label, board module name, Odds API sport key, accent color)
# The board module must expose the shared contract: a RATINGS dict with a
# "loaded_at" sentinel, load_all_ratings(), and
#   fetch_and_project() -> (live, upcoming, completed, date_display, error)
# with per-game fields away_name/home_name/away_abbrev/home_abbrev/state/
# proj_spread/proj_total/status_detail/away_score/home_score.
LEAGUES = {
    "wnba": ("WNBA", "wnba_live_projections", "basketball_wnba", "#c2185b"),
    "nba": ("NBA", "nba_live_projections", "basketball_nba", "#ff6d00"),
    "cbb": ("CBB", "cbb_live_projections", "basketball_ncaab", "#ff4444"),
    "wcbb": ("WCBB", "wcbb_live_projections", "basketball_wncaab", "#e040a0"),
}

# Only surface a row if its strongest edge is at least this many points.
MIN_SHOW_EDGE = ml.MILD_EDGE          # 1.0

CACHE_TTL = 40
_cache_lock = threading.Lock()
_cache: dict = {"rows": None, "meta": None, "ts": 0.0, "date": None}

DATE_OVERRIDE: str | None = None


# ── data assembly ─────────────────────────────────────────────────────────────
def _board_games(module_name: str):
    """(live, upcoming) projected games for one board, ratings ensured.

    Returns ([], []) on any failure so one broken/dark league can't take the
    page down.
    """
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        print(f"[edges] import {module_name} failed: {e}")
        return [], []
    try:
        if DATE_OVERRIDE is not None and hasattr(mod, "DATE_OVERRIDE"):
            mod.DATE_OVERRIDE = DATE_OVERRIDE
        if hasattr(mod, "RATINGS") and not mod.RATINGS.get("loaded_at"):
            mod.load_all_ratings()
        live, upcoming, _completed, _disp, _err = mod.fetch_and_project()
        return live or [], upcoming or []
    except Exception as e:
        print(f"[edges] {module_name} fetch_and_project failed: {e}")
        return [], []


def _row_for(game: dict, prefix: str, label: str, color: str, line: dict) -> dict | None:
    """Build one ranked edge row from a projected game + its market line."""
    proj_spread = game.get("proj_spread")
    proj_total = game.get("proj_total")
    if proj_spread is None and proj_total is None:
        return None

    sp_mag, sp_side, sp_cls = ml.spread_edge(proj_spread, line.get("spread_home"))
    tot_mag, tot_side, tot_cls = ml.total_edge(proj_total, line.get("total"))
    best = max(sp_mag or 0.0, tot_mag or 0.0)
    if best < MIN_SHOW_EDGE:
        return None

    state = game.get("state", "pre")
    away_ab = game.get("away_abbrev") or game.get("away_name", "AWY")
    home_ab = game.get("home_abbrev") or game.get("home_name", "HOM")
    if state == "in":
        status = game.get("status_detail", "LIVE")
        score = f'{game.get("away_score", 0)}-{game.get("home_score", 0)}'
    else:
        status = game.get("status_detail", "") or game.get("time_display", "")
        score = ""

    sh = line.get("spread_home")
    mkt_spread_disp = (
        "PK" if sh is not None and abs(sh) < 0.05
        else (f'{home_ab} {sh:+.1f}' if sh is not None else "—")
    )
    proj_spread_disp = (
        f'{home_ab} {-proj_spread:+.1f}' if proj_spread is not None else "—"
    )
    # Stable id + the game's own date (may be tomorrow within the 48h window),
    # so a grader can look up the final score later.
    epoch = game.get("start_epoch")
    if epoch and ET is not None:
        game_date = datetime.fromtimestamp(epoch, ET).date().isoformat()
    else:
        game_date = DATE_OVERRIDE or (
            datetime.now(ET).date().isoformat() if ET is not None
            else datetime.now().date().isoformat())
    return {
        "prefix": prefix, "league": label, "color": color,
        "game_id": game.get("game_id", ""), "game_date": game_date,
        "state": state, "status": status, "score": score,
        "away": away_ab, "home": home_ab,
        "away_name": game.get("away_name", ""), "home_name": game.get("home_name", ""),
        "matchup": f"{away_ab} @ {home_ab}",
        "proj_spread": proj_spread, "proj_total": proj_total,
        "mkt_spread_home": sh, "mkt_total": line.get("total"),
        "book_count": line.get("book_count", 0),
        "proj_spread_disp": proj_spread_disp,
        "proj_total_disp": (f'{proj_total:.1f}' if proj_total is not None else "—"),
        "mkt_spread_disp": mkt_spread_disp,
        "mkt_total_disp": (f'{line["total"]:.1f}' if line.get("total") is not None else "—"),
        "sp_edge_mag": sp_mag, "sp_edge_side": sp_side, "sp_edge_cls": sp_cls,
        "sp_edge_disp": (f'{sp_side} +{sp_mag:.1f}' if sp_mag is not None else ""),
        "tot_edge_mag": tot_mag, "tot_edge_side": tot_side, "tot_edge_cls": tot_cls,
        "tot_edge_disp": (f'{tot_side} +{tot_mag:.1f}' if tot_mag is not None else ""),
        "best_edge": best,
    }


def build_edges() -> tuple[list[dict], dict]:
    """All matched edge rows across leagues, ranked best-edge first, + meta."""
    now = time.monotonic()
    with _cache_lock:
        if (_cache["rows"] is not None and now - _cache["ts"] < CACHE_TTL
                and _cache["date"] == DATE_OVERRIDE):
            return _cache["rows"], _cache["meta"]

    rows: list[dict] = []
    league_counts: dict[str, int] = {}
    leagues_with_games = 0
    for prefix, (label, module_name, sport_key, color) in LEAGUES.items():
        # Cheap first: if the books have no lines for this league (off-season /
        # no key / quota), there can be no edges — so skip the board entirely
        # and DON'T trigger its ratings scrape. This is what keeps /edges fast
        # in July, when calling the CBB/WCBB boards would scrape WarrenNolan
        # only to return zero games.
        lines = ml.fetch_consensus(sport_key)
        if not lines:
            continue
        live, upcoming = _board_games(module_name)
        games = live + upcoming
        if games:
            leagues_with_games += 1
        if not games:
            continue
        n = 0
        for g in games:
            line = ml.match_line(g.get("away_name", ""), g.get("home_name", ""), lines)
            if not line:
                continue
            row = _row_for(g, prefix, label, color, line)
            if row:
                rows.append(row)
                n += 1
        if n:
            league_counts[prefix] = n

    # Live games first, then by strongest edge.
    rows.sort(key=lambda r: (r["state"] != "in", -r["best_edge"]))

    meta = {
        "updated": _now_str(),
        "row_count": len(rows),
        "league_counts": league_counts,
        "leagues_with_games": leagues_with_games,
        "quota": ml.quota(),
        "strong_edge": ml.STRONG_EDGE,
    }
    with _cache_lock:
        _cache.update(rows=rows, meta=meta, ts=now, date=DATE_OVERRIDE)
    return rows, meta


def _now_str() -> str:
    if ET is not None:
        return datetime.now(ET).strftime("%I:%M:%S %p ET")
    return datetime.now().strftime("%H:%M:%S")


# ── HTML rendering ────────────────────────────────────────────────────────────
def _chip(disp: str, cls: str) -> str:
    if not disp:
        return '<span class="chip edge-none">—</span>'
    return f'<span class="chip {cls}">{disp}</span>'


def rows_html(rows: list[dict]) -> str:
    if not rows:
        return ('<div class="empty">No market-vs-model edges right now. '
                'This lights up when a league is in season and the books have '
                'posted lines — today that means WNBA (and NBA/CBB/WCBB once '
                'their seasons tip off).</div>')
    out = []
    for r in rows:
        live = r["state"] == "in"
        badge = (f'<span class="live">● LIVE</span>' if live
                 else f'<span class="pre">{r["status"]}</span>')
        score = f'<span class="score">{r["score"]}</span>' if live and r["score"] else ""
        status_extra = f'<span class="statusd">{r["status"]}</span>' if live else ""
        out.append(f"""
<div class="row" data-league="{r['prefix']}" data-edge="{r['best_edge']:.2f}">
  <div class="c-league"><span class="lg" style="background:{r['color']}">{r['league']}</span></div>
  <div class="c-game">
    <div class="mu">{r['away_name']} <span class="at">@</span> {r['home_name']}</div>
    <div class="sub">{badge} {score} {status_extra}</div>
  </div>
  <div class="c-mkt">
    <div class="pair"><span class="k">Spread</span>
      <span class="model">{r['proj_spread_disp']}</span>
      <span class="vs">vs</span>
      <span class="book">{r['mkt_spread_disp']}</span>
      {_chip(r['sp_edge_disp'], r['sp_edge_cls'])}</div>
    <div class="pair"><span class="k">Total</span>
      <span class="model">{r['proj_total_disp']}</span>
      <span class="vs">vs</span>
      <span class="book">{r['mkt_total_disp']}</span>
      {_chip(r['tot_edge_disp'], r['tot_edge_cls'])}</div>
  </div>
</div>""")
    return "\n".join(out)


def league_filter_html(meta: dict) -> str:
    counts = meta.get("league_counts", {})
    if not counts:
        return ""
    chips = ['<button class="fbtn active" data-f="all">All</button>']
    for prefix, (label, _m, _k, color) in LEAGUES.items():
        if prefix in counts:
            chips.append(
                f'<button class="fbtn" data-f="{prefix}" '
                f'style="border-color:{color}">{label} '
                f'<b>{counts[prefix]}</b></button>')
    return "".join(chips)


def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        rows, meta = build_edges()
        q = meta["quota"].get("remaining")
        quota_note = f" · Odds API credits left: {q}" if q else ""
        return PAGE_HTML.replace("{{ROWS}}", rows_html(rows)) \
                        .replace("{{FILTERS}}", league_filter_html(meta)) \
                        .replace("{{UPDATED}}", meta["updated"]) \
                        .replace("{{COUNT}}", str(meta["row_count"])) \
                        .replace("{{STRONG}}", f'{meta["strong_edge"]:.0f}') \
                        .replace("{{QUOTA}}", quota_note)

    @app.route("/api/edges")
    def api_edges():
        rows, meta = build_edges()
        return jsonify({
            "rows_html": rows_html(rows),
            "filters_html": league_filter_html(meta),
            "count": meta["row_count"],
            "updated": meta["updated"],
            "quota": meta["quota"],
        })

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON_SVG, 200, {"Content-Type": "image/svg+xml"})

    return app


FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='6' fill='#0f1923'/>"
    "<path d='M6 22 L13 14 L18 18 L26 8' fill='none' stroke='#22c55e' "
    "stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'/>"
    "<circle cx='26' cy='8' r='2.4' fill='#22c55e'/></svg>"
)

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#0f1923"/>
<title>Today's Edges — Model vs Market</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"/>
<style>
  :root{--bg:#0f1923;--panel:#17222e;--panel2:#1c2836;--line:#2a3a4a;
    --txt:#e8edf2;--mut:#8ea0b2;--grn:#22c55e;--grn2:#16341f;
    --amb:#f6c343;--amb2:#3a2f10;--blu:#4ea3ff;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{position:sticky;top:0;z-index:10;background:linear-gradient(180deg,#132030,#0f1923);
    border-bottom:2px solid var(--grn);padding:14px 18px}
  .hrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .logo{font-weight:800;font-size:22px;letter-spacing:-.5px}
  .logo .g{color:var(--grn)}
  .sub{color:var(--mut);font-size:13px}
  .wrap{max-width:1080px;margin:0 auto;padding:16px}
  .bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 14px}
  .updated{color:var(--mut);font-size:12px;margin-left:auto}
  .filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .fbtn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
    border-radius:999px;padding:5px 12px;font-size:13px;cursor:pointer}
  .fbtn.active{background:var(--grn);border-color:var(--grn);color:#04140a;font-weight:700}
  .fbtn b{opacity:.7;font-weight:700}
  .minrow{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:12px;margin-bottom:10px}
  .minrow input{accent-color:var(--grn)}
  .row{display:grid;grid-template-columns:64px 1fr minmax(300px,1.2fr);gap:12px;
    align-items:center;background:var(--panel);border:1px solid var(--line);
    border-radius:12px;padding:11px 13px;margin-bottom:9px}
  .lg{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.5px;
    color:#fff;padding:3px 8px;border-radius:6px}
  .c-game .mu{font-weight:700;font-size:15px}
  .c-game .at{color:var(--mut);font-weight:400;margin:0 2px}
  .c-game .sub{color:var(--mut);font-size:12px;margin-top:3px;display:flex;gap:8px;align-items:center}
  .c-game .live{color:var(--grn);font-weight:700}
  .c-game .pre{color:var(--mut)}
  .c-game .score{color:var(--txt);font-weight:700}
  .c-game .statusd{color:var(--mut)}
  .c-mkt{display:flex;flex-direction:column;gap:6px}
  .pair{display:flex;align-items:center;gap:7px;font-size:13px;flex-wrap:wrap}
  .pair .k{color:var(--mut);width:46px;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .pair .model{font-weight:700}
  .pair .vs{color:var(--mut);font-size:11px}
  .pair .book{color:var(--mut)}
  .chip{margin-left:auto;font-size:12px;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap}
  .edge-strong{background:var(--grn2);color:var(--grn);border:1px solid #2a5a37}
  .edge-mild{background:var(--amb2);color:var(--amb);border:1px solid #5a4a18}
  .edge-none{background:transparent;color:var(--mut);border:1px solid var(--line)}
  .empty{color:var(--mut);text-align:center;padding:40px 20px;border:1px dashed var(--line);
    border-radius:12px;line-height:1.6}
  .foot{color:var(--mut);font-size:11px;text-align:center;margin:22px 0 14px;line-height:1.7}
  .foot b{color:var(--txt)}
  a{color:var(--blu);text-decoration:none}
  @media(max-width:680px){
    .row{grid-template-columns:52px 1fr;}
    .c-mkt{grid-column:1 / -1;border-top:1px dashed var(--line);padding-top:8px}
  }
</style>
</head>
<body>
<header>
  <div class="hrow">
    <div class="logo">TODAY'S <span class="g">EDGES</span></div>
    <div class="sub">Model projection vs. market consensus — ranked across every league</div>
  </div>
</header>
<div class="wrap">
  <div class="bar">
    <div class="filters" id="filters">{{FILTERS}}</div>
    <span class="updated" id="updated">Updated {{UPDATED}}{{QUOTA}}</span>
  </div>
  <div class="minrow">
    <label>Min edge: <b id="minval">1.0</b> pts</label>
    <input type="range" id="minedge" min="0" max="6" step="0.5" value="1"/>
    <span>· <b id="count">{{COUNT}}</b> games with an edge · green chip = edge ≥ {{STRONG}} pts</span>
  </div>
  <div id="rows">{{ROWS}}</div>
  <div class="foot">
    <b>Spread</b> shown from the home team's side (e.g. <b>LAS -3.5</b> = home laying 3.5).
    <b>Edge</b> = model minus market: a spread edge of <b>Home +2.0</b> means the model makes the
    home side 2 points better than the book's number; <b>Over +3.0</b> means the model's total is
    3 points above the posted total. Consensus = median across sharp books.
    Edges are a screen, not a bet — line-shop and check the juice.
    Data: The Odds API + each board's live projection engine · Auto-refreshes every 60s.
  </div>
</div>
<script>
let MIN_EDGE = 1.0;
let LEAGUE = 'all';
function applyFilters(){
  document.querySelectorAll('#rows .row').forEach(function(row){
    const e = parseFloat(row.dataset.edge || '0');
    const lg = row.dataset.league;
    const show = e >= MIN_EDGE && (LEAGUE === 'all' || lg === LEAGUE);
    row.style.display = show ? '' : 'none';
  });
}
function wireFilters(){
  document.querySelectorAll('#filters .fbtn').forEach(function(b){
    b.onclick = function(){
      document.querySelectorAll('#filters .fbtn').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      LEAGUE = b.dataset.f;
      applyFilters();
    };
  });
}
document.getElementById('minedge').addEventListener('input', function(e){
  MIN_EDGE = parseFloat(e.target.value);
  document.getElementById('minval').textContent = MIN_EDGE.toFixed(1);
  applyFilters();
});
wireFilters();
applyFilters();
async function refresh(){
  try{
    const r = await fetch('api/edges', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('rows').innerHTML = d.rows_html;
    document.getElementById('filters').innerHTML = d.filters_html;
    document.getElementById('count').textContent = d.count;
    const q = d.quota && d.quota.remaining ? ' · Odds API credits left: '+d.quota.remaining : '';
    document.getElementById('updated').textContent = 'Updated ' + d.updated + q;
    wireFilters();
    // keep current league selection highlighted
    document.querySelectorAll('#filters .fbtn').forEach(x=>{
      x.classList.toggle('active', x.dataset.f === LEAGUE);
    });
    applyFilters();
  }catch(e){/* keep last-good */}
}
setInterval(refresh, 60000);
</script>
</body>
</html>"""


app = create_app()


def run_once():
    rows, meta = build_edges()
    print(f"\nTODAY'S EDGES  [updated {meta['updated']}]  "
          f"{meta['row_count']} games with an edge  "
          f"(Odds API remaining {meta['quota'].get('remaining')})")
    if not rows:
        print("  (no matched model-vs-market edges right now)")
        return
    for r in rows:
        tag = "LIVE" if r["state"] == "in" else r["status"]
        print(f"\n  [{r['league']}] {r['away_name']} @ {r['home_name']}  ({tag})")
        print(f"    spread:  model {r['proj_spread_disp']:>12s}  |  "
              f"market {r['mkt_spread_disp']:>12s}  |  edge {r['sp_edge_disp'] or '—'}")
        print(f"    total:   model {r['proj_total_disp']:>12s}  |  "
              f"market {r['mkt_total_disp']:>12s}  |  edge {r['tot_edge_disp'] or '—'}")


def main():
    global DATE_OVERRIDE
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Cross-league Today's Edges aggregator")
    ap.add_argument("--port", type=int, default=5015)
    ap.add_argument("--date", help="focus date YYYY-MM-DD (drives every board)")
    ap.add_argument("--once", action="store_true", help="print ranked edges and exit")
    args = ap.parse_args()
    if args.date:
        DATE_OVERRIDE = args.date
    if args.once:
        run_once()
        return
    print(f"Today's Edges → http://localhost:{args.port}")
    application = create_app()
    application.run(host="0.0.0.0", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
