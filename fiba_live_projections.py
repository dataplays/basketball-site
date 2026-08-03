#!/usr/bin/env python3
"""FIBA National-Team Live Projections — its own /fiba tab.

Senior national-team basketball (men + women): World Cup and its qualifying
windows, EuroBasket, Asia Cup, the Olympics, and international friendlies.
Thin wrapper over the International board's engine — same projections,
self-computed ratings, scoreboard fetch, templates and partials — showing
ONLY intl.FIBA_SLUGS. Auto-mounts at /fiba (matches *_live_projections.py).

Worth knowing about this board specifically:
  * api-sports has no separate "qualifiers" competition. Qualifying windows
    live INSIDE the championship season, so "FIBA World Cup" covers the whole
    2027 cycle -- 454 games from Feb 2024 through Mar 2027.
  * National-team samples are SHORT (a team plays a handful of games per
    cycle), so ratings are thin and early-tournament projections lean toward
    league average. Treat them as a starting point, not a sharp number.
  * Home edge is set per FORMAT, not per league: home-and-away qualifiers get
    2.5, single-host tournaments 0, friendlies 2.0.

Run standalone:  py -3 fiba_live_projections.py [--port 5024] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template_string, url_for

import intl_live_projections as il

app = Flask(__name__)

# Reuse intl's full page (CSS + game partials), rebranded for FIBA.
PAGE = (il.HTML_TEMPLATE
        .replace("International Basketball Live Projections",
                 "FIBA National-Team Live Projections")
        .replace("&#127760;", "&#127942;"))          # globe -> trophy


def _slugs():
    return il.FIBA_SLUGS


@app.route("/")
def index():
    il.ensure_ratings_loading()
    live, upcoming, completed, date_display, error, league_summary, league_count = \
        il.fetch_and_project(only=_slugs())
    return il._render_with_partials(
        PAGE,
        live=live, upcoming=upcoming, completed=completed, games=live,
        date_display=date_display,
        total_games=len(live) + len(upcoming) + len(completed),
        league_summary=league_summary, league_count=league_count,
        leagues_checked=", ".join(
            il.LEAGUES[s]["name"] for s in sorted(_slugs()) if s in il.LEAGUES),
        no_games_at_all=(len(live) + len(upcoming) + len(completed) == 0),
        euro_leagues=[],
        ratings_time=il.RATINGS_LOADED_AT.strftime("%I:%M %p ET") if il.RATINGS_LOADED_AT else "N/A",
        error=error,
    )


@app.route("/api/games")
def api_games():
    il.ensure_ratings_loading()
    live, upcoming, completed, _, error, _, _ = il.fetch_and_project(only=_slugs())
    return jsonify({
        "live_html": render_template_string(il.LIVE_PARTIAL, games=live),
        "upcoming_html": render_template_string(il.UPCOMING_PARTIAL, upcoming=upcoming),
        "completed_html": render_template_string(il.COMPLETED_PARTIAL, completed=completed),
        "live_count": len(live), "upcoming_count": len(upcoming),
        "completed_count": len(completed),
        "updated_at": datetime.now(il.ET).strftime("%I:%M:%S %p ET"),
        "error": error,
        "apisports_error": il.APISPORTS_LAST_ERROR,
        "fiba_configured": sorted(_slugs()),
    })


@app.route("/refresh")
def refresh_ratings():
    with il._ratings_thread_lock:
        il.RATINGS_LOADED_AT = None
        il._ratings_thread = None
    il.ensure_ratings_loading()
    return redirect(url_for("index"))


def main():
    ap = argparse.ArgumentParser(description="FIBA National-Team Live Projections")
    ap.add_argument("--port", type=int, default=5024)
    ap.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = ap.parse_args()
    if args.date:
        il.DATE_OVERRIDE = args.date
    il.load_all_ratings()
    print("=" * 58)
    print("  FIBA National-Team Live Projections")
    print("=" * 58)
    print(f"  World Cup / EuroBasket / Asia Cup / Olympics / friendlies")
    print(f"  -> http://localhost:{args.port}")
    print("  (reuses the International board's engine + self-computed ratings)")
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
