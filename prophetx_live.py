#!/usr/bin/env python3
r"""
prophetx_live.py - Live web dashboard for ProphetX exchange basketball lines.

Shows every ProphetX market with the odds AND the money offered at each price
(the exchange `limit`, in USD), grouped by game -> game lines / player props,
with a liquidity bar and a click-through to the ProphetX betslip.

Reuses the data engine in prophetx_lines.py (gather()). Catalog/player lookups
are cached there; this server caches the assembled slate for CACHE_TTL seconds
so the 30s client auto-refresh doesn't burn API quota.

Run (PowerShell):
    $env:ODDSPAPI_KEY = "your_rapidapi_key"
    py -3 prophetx_live.py                 # http://localhost:5007
    py -3 prophetx_live.py --port 8080
"""
from __future__ import annotations

import argparse
import os
import time

from flask import Flask, jsonify, request

import prophetx_lines as px

KEY = os.environ.get("ODDSPAPI_KEY", "")
CACHE_TTL = 25.0           # seconds to reuse an assembled slate
ACCENT = "#15c39a"

# Books we expose. Exchanges (prophetx/kalshi) carry real `limit` size, so they
# get liquidity bars + the kappa-shaded line. Traditional sportsbooks post odds
# only (no size) -> we show their odds + the no-vig fair line, no bar/shade.
EXCHANGES = {"prophetx", "kalshi"}
SPORTSBOOKS = {"caesars", "betrivers", "thescore", "fanduel"}
BOOKS = EXCHANGES | SPORTSBOOKS
# Book selector buttons (label -> view); "compare" is a special PX-vs-Kalshi mode.
BOOK_CHIPS = [("ProphetX", "prophetx"), ("Kalshi", "kalshi"),
              ("Caesars", "caesars"), ("BetRivers", "betrivers"),
              ("theScore", "thescore"), ("FanDuel", "fanduel"),
              ("Compare", "compare")]

# Tournament quick-filters shown in the UI (label -> OddsPapi tournamentId, 0=all).
TOURNAMENTS = [("All", 0), ("WNBA", 486), ("NBA", 132), ("Summer League", 15822)]

app = Flask(__name__)
_cache: dict = {}          # (tournamentId, book) -> (ts, games)


def cached_games(tournament: int, book: str = "prophetx"):
    now = time.time()
    hit = _cache.get((tournament, book))
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1], hit[0]
    games = px.gather(KEY, sport=11, tournament=tournament or None,
                      book=book, min_limit=0.0)
    _cache[(tournament, book)] = (now, games)
    return games, now


def moneyline_sides(game: dict) -> dict:
    """team name -> the moneyline outcome dict, or {} if the game has no ML."""
    for m in game["markets"]:
        if m.get("mtype") == "moneyline":
            return {o["sel"]: o for o in m["outcomes"]}
    return {}


def build_compare(tournament: int):
    """Match ProphetX vs Kalshi on the same fixture and pair up moneylines.

    fixtureId is OddsPapi's own (book-independent), so it's the join key. For each
    game both books price, we emit one row per side with both books' price + size
    and mark which book offers the better (higher-decimal) price.
    """
    px_games, ts1 = cached_games(tournament, "prophetx")
    k_games, ts2 = cached_games(tournament, "kalshi")
    k_by_id = {g["fixture_id"]: g for g in k_games}

    out = []
    for pg in px_games:
        kg = k_by_id.get(pg["fixture_id"])
        if not kg:
            continue
        px_ml, k_ml = moneyline_sides(pg), moneyline_sides(kg)
        if not px_ml or not k_ml:
            continue
        sides = []
        for team in px_ml:
            po, ko = px_ml.get(team), k_ml.get(team)
            if not po or not ko:
                continue
            pd, kd = po["decimal"], ko["decimal"]
            best = "tie" if abs(pd - kd) < 1e-9 else ("px" if pd > kd else "k")
            sides.append({
                "team": team,
                "px_decimal": pd, "px_american": po.get("american"),
                "px_limit": po["limit"], "px_betslip": po.get("betslip", ""),
                "k_decimal": kd, "k_american": ko.get("american"),
                "k_limit": ko["limit"],
                "best": best,
                "edge_pct": round(abs(pd - kd) / min(pd, kd) * 100, 2),
            })
        if len(sides) >= 2:
            out.append({
                "fixture_id": pg["fixture_id"], "game": pg["game"],
                "tournament": pg["tournament"], "status": pg["status"],
                "live": pg["live"], "start_epoch": pg["start_epoch"],
                "sides": sides,
            })
    out.sort(key=lambda x: (not x["live"], x["start_epoch"] or 0))
    return out, min(ts1, ts2)


def attach_fair(games: list, kappa: float, by_liability: bool = False) -> list:
    """Attach a fair/shaded line to each game's 2-way moneyline market (copy)."""
    out = []
    for g in games:
        markets = []
        for m in g["markets"]:
            if m.get("mtype") == "moneyline" and len(m["outcomes"]) == 2:
                a, b = m["outcomes"]
                f = px.fair_no_vig(a["decimal"], a["limit"], b["decimal"],
                                   b["limit"], kappa=kappa, by_liability=by_liability)
                if f:
                    m = {**m, "fair": {
                        "overround": f["overround"], "lean_a": f["lean_a"],
                        "sides": [
                            {"sel": a["sel"], "offered": a["american"],
                             "fair": f["fair_a"], "shaded": f["shaded_a"],
                             "limit": a["limit"], "betslip": a.get("betslip", "")},
                            {"sel": b["sel"], "offered": b["american"],
                             "fair": f["fair_b"], "shaded": f["shaded_b"],
                             "limit": b["limit"], "betslip": b.get("betslip", "")},
                        ]}}
            markets.append(m)
        out.append({**g, "markets": markets})
    return out


def filter_min_limit(games: list, min_limit: float) -> list:
    """Return a copy with sub-threshold prices (and emptied markets) dropped."""
    if min_limit <= 0:
        return games
    out = []
    for g in games:
        markets = []
        for m in g["markets"]:
            keep = [o for o in m["outcomes"] if o["limit"] >= min_limit]
            if keep:
                markets.append({**m, "outcomes": keep})
        if markets:
            out.append({**g, "markets": markets,
                        "n_lines": sum(len(m["outcomes"]) for m in markets)})
    return out


@app.route("/api/lines")
def api_lines():
    if not KEY:
        return jsonify(ok=False, error="ODDSPAPI_KEY not set on the server."), 200
    try:
        tournament = int(request.args.get("tournament", 0) or 0)
    except ValueError:
        tournament = 0
    try:
        min_limit = float(request.args.get("min_limit", 0) or 0)
    except ValueError:
        min_limit = 0.0
    book = request.args.get("book", "prophetx")
    if book not in BOOKS:
        book = "prophetx"
    try:
        kappa = float(request.args.get("kappa", 0) or 0)
    except ValueError:
        kappa = 0.0
    kappa = max(-1.0, min(1.0, kappa))
    by_liability = request.args.get("weight", "stake") == "liability"
    try:
        games, ts = cached_games(tournament, book)
    except px.OddsPapiError as exc:
        return jsonify(ok=False, error=str(exc)), 200
    if book in EXCHANGES:                 # sportsbooks have no size to filter on
        games = filter_min_limit(games, min_limit)
    games = attach_fair(games, kappa, by_liability)
    return jsonify(ok=True, updated=ts, book=book, kappa=kappa,
                   exchange=(book in EXCHANGES),
                   count=sum(g["n_lines"] for g in games), games=games)


@app.route("/api/compare")
def api_compare():
    if not KEY:
        return jsonify(ok=False, error="ODDSPAPI_KEY not set on the server."), 200
    try:
        tournament = int(request.args.get("tournament", 0) or 0)
    except ValueError:
        tournament = 0
    try:
        min_limit = float(request.args.get("min_limit", 0) or 0)
    except ValueError:
        min_limit = 0.0
    try:
        games, ts = build_compare(tournament)
    except px.OddsPapiError as exc:
        return jsonify(ok=False, error=str(exc)), 200
    if min_limit > 0:
        games = [{**g, "sides": [s for s in g["sides"]
                                 if s["px_limit"] >= min_limit and s["k_limit"] >= min_limit]}
                 for g in games]
        games = [g for g in games if len(g["sides"]) >= 1]
    return jsonify(ok=True, updated=ts, count=sum(len(g["sides"]) for g in games),
                   games=games)


@app.route("/api/snapshot", methods=["GET", "POST"])
def api_snapshot():
    """GET -> snapshot-log stats; POST -> log the current slate's moneylines.

    Writes via px.append_ml_snapshot to prophetx_ml_log.csv (feeds the kappa
    calibration). Persists only where the filesystem does -- run locally."""
    if not KEY:
        return jsonify(ok=False, error="ODDSPAPI_KEY not set on the server."), 200
    if request.method == "GET":
        return jsonify(ok=True, **px.ml_log_stats())
    book = request.args.get("book", "prophetx")
    if book not in BOOKS:
        book = "prophetx"
    try:
        tournament = int(request.args.get("tournament", 0) or 0)
    except ValueError:
        tournament = 0
    try:
        games, _ = cached_games(tournament, book)
    except px.OddsPapiError as exc:
        return jsonify(ok=False, error=str(exc)), 200
    n = px.append_ml_snapshot(games, book=book)
    return jsonify(ok=True, logged=n, **px.ml_log_stats())


@app.route("/favicon.svg")
def favicon():
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           f'<rect width="32" height="32" rx="7" fill="{ACCENT}"/>'
           f'<text x="16" y="22" font-size="18" font-family="Arial" font-weight="bold" '
           f'text-anchor="middle" fill="#0e1116">P</text></svg>')
    return app.response_class(svg, mimetype="image/svg+xml")


@app.route("/")
def index():
    chips = "".join(
        f'<button class="chip" data-tid="{tid}">{label}</button>'
        for label, tid in TOURNAMENTS)
    books = "".join(
        f'<button class="bchip" data-view="{view}">{label}</button>'
        for label, view in BOOK_CHIPS)
    return (PAGE.replace("__CHIPS__", chips).replace("__BOOKS__", books)
                .replace("__ACCENT__", ACCENT))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProphetX Exchange Lines</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{ --accent:__ACCENT__; --bg:#0d1117; --card:#161b22; --row:#1c2230;
       --line:#283040; --txt:#e6edf3; --muted:#8b949e; --money:#3fb950; }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
     font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(13,17,23,.95);
       backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:14px 20px}
.titlerow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:0;letter-spacing:.3px}
h1 b{color:var(--accent)}
.sub{color:var(--muted);font-size:12.5px}
.controls{display:flex;gap:10px;align-items:center;margin-top:11px;flex-wrap:wrap}
.chip{background:var(--row);color:var(--txt);border:1px solid var(--line);
      border-radius:999px;padding:6px 14px;font-size:13px;cursor:pointer}
.chip.active{background:var(--accent);color:#08130f;border-color:var(--accent);font-weight:600}
.bchip{background:transparent;color:var(--muted);border:1px solid var(--line);
       border-radius:8px;padding:6px 13px;font-size:13px;cursor:pointer;font-weight:600}
.bchip.active{background:#1f6feb22;color:#58a6ff;border-color:#1f6feb88}
.bchip.snap{color:var(--accent);border-color:#15c39a55}
.bchip.snap:hover{background:#15c39a18}
.bchip.snap:disabled{opacity:.55;cursor:default}
.bookrow{display:flex;gap:8px;align-items:center;margin-top:11px}
.bookrow .lbl{color:var(--muted);font-size:12px;margin-right:2px}
/* compare table */
.cmp{width:100%;border-collapse:collapse;font-size:13px}
.cmp th{font-size:10.5px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);
        text-align:right;padding:4px 10px;font-weight:600}
.cmp th.tm{text-align:left}
.cmp td{padding:7px 10px;border-top:1px solid var(--line);text-align:right;
        font-variant-numeric:tabular-nums}
.cmp td.tm{text-align:left;font-size:13.5px}
.cmp td a{color:inherit;text-decoration:none}
.cmp td a:hover{color:var(--accent)}
.cmp .am{color:var(--muted);font-size:11.5px;margin-left:4px}
.cmp .amt{color:var(--money);font-weight:600}
.cmp .best{color:#3fb950;font-weight:700}
.cmp .win{font-size:10.5px;border-radius:5px;padding:1px 6px;margin-left:6px;
          background:#3fb95022;color:#3fb950;border:1px solid #3fb95055}
/* fair / shaded moneyline table */
.fair{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px}
.fair th{font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);
         text-align:right;padding:3px 10px;font-weight:600}
.fair th.tm{text-align:left}
.fair td{padding:6px 10px;border-top:1px solid var(--line);text-align:right;
         font-variant-numeric:tabular-nums}
.fair td.tm{text-align:left;font-size:13.5px}
.fair td a{color:inherit;text-decoration:none}
.fair td a:hover{color:var(--accent)}
.fair .off{color:var(--muted)}
.fair .fr{color:var(--txt);font-weight:600}
.fair .sh{color:var(--accent);font-weight:700}
.fair .amt{color:var(--money);font-weight:600}
.fmeta{font-size:11px;color:var(--muted);margin:2px 2px 2px}
.fmeta b{color:#c9d4e3}
.minl{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;margin-left:auto}
.minl input{width:80px;background:var(--row);border:1px solid var(--line);color:var(--txt);
            border-radius:7px;padding:5px 8px;font-size:13px}
.upd{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:7px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--accent);opacity:.35}
.dot.on{opacity:1;box-shadow:0 0 0 4px rgba(21,195,154,.18);transition:.2s}
main{max-width:1060px;margin:0 auto;padding:18px 16px 60px}
.game{background:var(--card);border:1px solid var(--line);border-radius:13px;
      margin-bottom:18px;overflow:hidden}
.ghead{display:flex;align-items:center;gap:11px;padding:13px 16px;border-bottom:1px solid var(--line)}
.gteams{font-size:16.5px;font-weight:650}
.gtag{font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 7px}
.badge{font-size:11px;font-weight:700;border-radius:6px;padding:2px 8px;text-transform:uppercase;letter-spacing:.4px}
.badge.live{background:#3fb95022;color:#3fb950;border:1px solid #3fb95055}
.badge.pre{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb55}
.sec{padding:6px 16px 14px}
.sec h3{font-size:11.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);
        margin:14px 2px 8px}
.market{margin:0 0 11px}
.mhdr{font-size:13px;color:#c9d4e3;font-weight:600;margin:0 0 5px}
.row{display:grid;grid-template-columns:1fr auto 188px;gap:10px;align-items:center;
     background:var(--row);border:1px solid var(--line);border-radius:9px;
     padding:8px 12px;margin-bottom:5px;text-decoration:none;color:inherit}
.row:hover{border-color:var(--accent)}
.row.book{grid-template-columns:1fr auto}
.sel{font-size:13.5px}
.odds{font-variant-numeric:tabular-nums;font-size:13.5px;text-align:right;white-space:nowrap}
.odds .am{color:var(--muted);font-size:12px;margin-left:5px}
.liq{display:flex;align-items:center;gap:8px}
.bar{flex:1;height:7px;background:#0e1320;border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#2ea043,#3fb950)}
.amt{font-variant-numeric:tabular-nums;font-size:13px;color:var(--money);
     font-weight:650;white-space:nowrap;min-width:64px;text-align:right}
.empty,.err{color:var(--muted);text-align:center;padding:60px 20px;font-size:15px}
.err{color:#f0883e}
.foot{color:var(--muted);font-size:11.5px;text-align:center;margin-top:24px;line-height:1.6}
</style></head>
<body>
<header>
  <div class="titlerow">
    <h1>Prophet<b>X</b> Exchange Lines</h1>
    <span class="sub">live odds &amp; money offered &bull; via OddsPapi</span>
  </div>
  <div class="controls">
    __CHIPS__
    <span class="minl">min&nbsp;$<input id="minl" type="number" min="0" step="25" value="0"></span>
    <span class="minl" title="liquidity shade strength (log-odds); 0 = pure no-vig">&kappa;&nbsp;<input id="kap" type="number" min="-1" max="1" step="0.05" value="0"></span>
  </div>
  <div class="bookrow"><span class="lbl">book</span>__BOOKS__</div>
  <div class="bookrow">
    <button id="snap" class="bchip snap" title="log the current moneylines to prophetx_ml_log.csv for kappa calibration (persists locally)">&#10515; Log snapshot</button>
    <span id="snapst" class="lbl"></span>
  </div>
  <div class="controls" style="margin-top:8px">
    <span class="upd"><span class="dot" id="dot"></span><span id="upd">loading&hellip;</span></span>
  </div>
</header>
<main><div id="games"></div>
  <div class="foot">Exchanges (<b>ProphetX</b>, <b>Kalshi</b>) show the <b>USD available to
   match right now</b> per price (top-of-book) plus a &kappa;-shaded fair line.
   Sportsbooks (<b>Caesars</b>, <b>BetRivers</b>, <b>theScore</b>) post odds only &mdash; shown with a
   no-vig fair line, no liquidity.<br>
   <b>Compare</b> pits ProphetX vs Kalshi moneylines (Kalshi is moneyline-only for hoops).<br>
   Auto-refreshes every 30s. For entertainment/informational use.</div>
</main>
<script>
let TID = 0, MINL = 0, KAPPA = 0, VIEW = 'prophetx', EXCH = true, busy = false;

function amClass(a){ return a>0 ? '+'+a : ''+a; }
function amSpan(a){ return (typeof a==='number') ? '<span class="am">'+amClass(a)+'</span>' : ''; }
function fmtAm(a){ return (typeof a==='number') ? amClass(a) : '—'; }
function money(n){ return '$'+Math.round(n).toLocaleString(); }

function fairTable(m){
  const f = m.fair;
  const orr = (f.overround*100).toFixed(1);
  const cell = s=>{
    const href = s.betslip ? ' href="'+s.betslip+'" target="_blank" rel="noopener"' : '';
    return href ? '<a'+href+'>'+esc(s.sel)+'</a>' : esc(s.sel);
  };
  if(!EXCH){   // sportsbook: odds + no-vig fair only (no liquidity to shade)
    const rows = f.sides.map(s=>'<tr><td class="tm">'+cell(s)+'</td>'+
      '<td class="off">'+fmtAm(s.offered)+'</td>'+
      '<td class="fr">'+fmtAm(s.fair)+'</td></tr>').join('');
    return '<table class="fair"><thead><tr>'+
      '<th class="tm">Side</th><th>Offered</th><th>Fair (no-vig)</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table>'+
      '<div class="fmeta">overround <b>'+orr+'%</b> &middot; de-vigged fair line</div>';
  }
  const rows = f.sides.map(s=>'<tr><td class="tm">'+cell(s)+'</td>'+
    '<td class="off">'+fmtAm(s.offered)+'</td>'+
    '<td class="fr">'+fmtAm(s.fair)+'</td>'+
    '<td class="sh">'+fmtAm(s.shaded)+'</td>'+
    '<td class="amt">'+money(s.limit)+'</td></tr>').join('');
  const leanTeam = f.lean_a>0 ? f.sides[0].sel : f.sides[1].sel;
  const leanTxt = Math.abs(f.lean_a)<0.001 ? 'balanced liquidity'
                                           : ('money leans <b>'+esc(leanTeam)+'</b>');
  return '<table class="fair"><thead><tr>'+
    '<th class="tm">Side</th><th>Offered</th><th>Fair</th><th>Shaded</th><th>$ offered</th>'+
    '</tr></thead><tbody>'+rows+'</tbody></table>'+
    '<div class="fmeta">overround <b>'+orr+'%</b> &middot; '+leanTxt+
    ' &middot; &kappa;=<b>'+KAPPA+'</b></div>';
}

function render(d){
  const box = document.getElementById('games');
  const upd = document.getElementById('upd');
  if(!d.ok){ box.innerHTML = '<div class="err">'+(d.error||'Error loading lines')+'</div>'; upd.textContent='error'; return; }
  EXCH = (d.exchange !== false);
  const t = new Date(d.updated*1000);
  upd.textContent = d.count+' prices · updated '+t.toLocaleTimeString();
  if(!d.games.length){ box.innerHTML = '<div class="empty">No '+esc(d.book||'')+' basketball with odds right now.</div>'; return; }

  box.innerHTML = d.games.map(g=>{
    let maxL = 1;
    g.markets.forEach(m=>m.outcomes.forEach(o=>{ if(o.limit>maxL) maxL=o.limit; }));
    const lines = g.markets.filter(m=>!m.is_prop);
    const props = g.markets.filter(m=>m.is_prop);
    const badge = g.live ? '<span class="badge live">Live</span>'
                         : '<span class="badge pre">'+(g.status||'Upcoming')+'</span>';
    const rowsHtml = m => m.outcomes.map(o=>{
        const am = amSpan(o.american);
        const href = o.betslip ? ' href="'+o.betslip+'" target="_blank" rel="noopener"' : '';
        if(!EXCH){   // sportsbook: odds only, no liquidity column
          return '<a class="row book"'+href+'><span class="sel">'+esc(o.sel)+'</span>'+
            '<span class="odds">'+o.decimal+am+'</span></a>';
        }
        const w = Math.max(4, Math.round(o.limit/maxL*100));
        return '<a class="row"'+href+'><span class="sel">'+esc(o.sel)+'</span>'+
          '<span class="odds">'+o.decimal+am+'</span>'+
          '<span class="liq"><span class="bar"><i style="width:'+w+'%"></i></span>'+
          '<span class="amt">$'+Math.round(o.limit).toLocaleString()+'</span></span></a>';
      }).join('');
    const mkt = m => '<div class="market"><div class="mhdr">'+esc(m.header)+'</div>'+
      (m.fair ? fairTable(m) : rowsHtml(m))+'</div>';
    const sec = (title,arr)=> arr.length ? '<div class="sec"><h3>'+title+'</h3>'+arr.map(mkt).join('')+'</div>' : '';
    return '<div class="game"><div class="ghead"><span class="gteams">'+esc(g.game)+'</span>'+
      '<span class="gtag">'+esc(g.tournament)+'</span>'+badge+'</div>'+
      sec('Game Lines', lines)+sec('Player Props', props)+'</div>';
  }).join('');
}

function renderCompare(d){
  const box = document.getElementById('games');
  const upd = document.getElementById('upd');
  if(!d.ok){ box.innerHTML = '<div class="err">'+(d.error||'Error loading lines')+'</div>'; upd.textContent='error'; return; }
  const t = new Date(d.updated*1000);
  upd.textContent = d.count+' sides · ProphetX vs Kalshi · updated '+t.toLocaleTimeString();
  if(!d.games.length){ box.innerHTML = '<div class="empty">No games priced by <b>both</b> ProphetX and Kalshi right now.</div>'; return; }

  box.innerHTML = d.games.map(g=>{
    const badge = g.live ? '<span class="badge live">Live</span>'
                         : '<span class="badge pre">'+(g.status||'Upcoming')+'</span>';
    const rows = g.sides.map(s=>{
      const pxWin = s.best==='px' ? '<span class="win">+'+s.edge_pct+'%</span>' : '';
      const kWin  = s.best==='k'  ? '<span class="win">+'+s.edge_pct+'%</span>' : '';
      const pxCls = s.best==='px' ? 'best' : '';
      const kCls  = s.best==='k'  ? 'best' : '';
      const pxOdds = s.px_betslip
        ? '<a href="'+s.px_betslip+'" target="_blank" rel="noopener">'+s.px_decimal+amSpan(s.px_american)+'</a>'
        : s.px_decimal+amSpan(s.px_american);
      return '<tr><td class="tm">'+esc(s.team)+'</td>'+
        '<td class="'+pxCls+'">'+pxOdds+pxWin+'</td>'+
        '<td class="amt">'+money(s.px_limit)+'</td>'+
        '<td class="'+kCls+'">'+s.k_decimal+amSpan(s.k_american)+kWin+'</td>'+
        '<td class="amt">'+money(s.k_limit)+'</td></tr>';
    }).join('');
    return '<div class="game"><div class="ghead"><span class="gteams">'+esc(g.game)+'</span>'+
      '<span class="gtag">'+esc(g.tournament)+'</span>'+badge+'</div>'+
      '<div class="sec"><table class="cmp"><thead><tr>'+
      '<th class="tm">Side</th><th>ProphetX</th><th>$ size</th><th>Kalshi</th><th>$ size</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table></div></div>';
  }).join('');
}

function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function load(){
  if(busy || document.hidden) return; busy = true;
  const dot = document.getElementById('dot'); dot.classList.add('on');
  try{
    if(VIEW==='compare'){
      const r = await fetch('api/compare?tournament='+TID+'&min_limit='+MINL);
      renderCompare(await r.json());
    } else {
      const r = await fetch('api/lines?book='+VIEW+'&tournament='+TID+'&min_limit='+MINL+'&kappa='+KAPPA);
      render(await r.json());
    }
  }catch(e){ document.getElementById('upd').textContent='connection error'; }
  finally{ busy=false; setTimeout(()=>dot.classList.remove('on'), 350); }
}

document.querySelectorAll('.chip').forEach((c,i)=>{
  if(i===0) c.classList.add('active');
  c.onclick = ()=>{ document.querySelectorAll('.chip').forEach(x=>x.classList.remove('active'));
    c.classList.add('active'); TID = +c.dataset.tid; load(); };
});
document.querySelectorAll('.bchip').forEach((c,i)=>{
  if(i===0) c.classList.add('active');
  c.onclick = ()=>{ document.querySelectorAll('.bchip').forEach(x=>x.classList.remove('active'));
    c.classList.add('active'); VIEW = c.dataset.view; load(); };
});
document.getElementById('minl').addEventListener('change', e=>{ MINL = +e.target.value||0; load(); });
document.getElementById('kap').addEventListener('change', e=>{ KAPPA = +e.target.value||0; load(); });

function snapTxt(d){
  const t = d.last ? new Date(d.last*1000).toLocaleTimeString() : '—';
  return (d.games||0)+' games · '+(d.snapshots||0)+' snaps · last '+t;
}
async function snapStats(){
  try{ const r = await fetch('api/snapshot'); const d = await r.json();
    if(d.ok) document.getElementById('snapst').textContent = 'log: '+snapTxt(d);
  }catch(e){}
}
document.getElementById('snap').onclick = async ()=>{
  const b = document.getElementById('snap'), st = document.getElementById('snapst');
  const book = (VIEW==='kalshi') ? 'kalshi' : 'prophetx';
  b.disabled = true; const old = b.innerHTML; b.textContent = 'logging…';
  try{
    const r = await fetch('api/snapshot?book='+book+'&tournament='+TID, {method:'POST'});
    const d = await r.json();
    st.textContent = d.ok ? ('✓ logged '+d.logged+' ('+book+') · '+snapTxt(d))
                          : ('error: '+(d.error||''));
  }catch(e){ st.textContent = 'snapshot error'; }
  finally{ b.disabled = false; b.innerHTML = old; }
};
snapStats();
load();
setInterval(load, 30000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) load(); });
</script>
</body></html>"""


def main() -> None:
    global KEY
    ap = argparse.ArgumentParser(description="ProphetX live lines dashboard")
    ap.add_argument("--port", type=int, default=5007)
    ap.add_argument("--key", default=KEY, help="RapidAPI key (or ODDSPAPI_KEY env)")
    args = ap.parse_args()
    KEY = args.key
    if not KEY:
        print("WARNING: no key. Set $env:ODDSPAPI_KEY or pass --key (the page will show an error).")
    print(f"ProphetX live lines -> http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
