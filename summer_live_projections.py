#!/usr/bin/env python3
"""NBA Summer League Live Projections — its own /summer tab.

Summer League (Las Vegas + Salt Lake City) was split out of the International
board (/intl) into its own dashboard. This is a thin wrapper: it reuses the
intl engine — projections, self-computed ratings, scoreboard fetch, templates
and partials — but fetches/shows ONLY the summer leagues (intl.SUMMER_SLUGS)
and rebrands the page. Auto-mounts at /summer (matches *_live_projections.py).

Run standalone:  py -3 summer_live_projections.py [--port 5015] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template_string, url_for

import intl_live_projections as il

app = Flask(__name__)

# Reuse intl's full page (CSS + game partials), rebranded for Summer League.
PAGE = (il.HTML_TEMPLATE
        .replace("International Basketball Live Projections", "NBA Summer League Live Projections")
        .replace("&#127760;", "&#9728;&#65039;"))   # globe -> sun


@app.route("/")
def index():
    il.ensure_ratings_loading()
    live, upcoming, completed, date_display, error, league_summary, league_count = \
        il.fetch_and_project(only=il.SUMMER_SLUGS)
    return il._render_with_partials(
        PAGE,
        live=live, upcoming=upcoming, completed=completed, games=live,
        date_display=date_display,
        total_games=len(live) + len(upcoming) + len(completed),
        league_summary=league_summary, league_count=league_count,
        leagues_checked=", ".join(il.LEAGUES[s]["name"] for s in il.SUMMER_SLUGS if s in il.LEAGUES),
        no_games_at_all=(len(live) + len(upcoming) + len(completed) == 0),
        euro_leagues=[],
        ratings_time=il.RATINGS_LOADED_AT.strftime("%I:%M %p ET") if il.RATINGS_LOADED_AT else "N/A",
        error=error,
    )


@app.route("/api/games")
def api_games():
    il.ensure_ratings_loading()
    live, upcoming, completed, _, error, _, _ = il.fetch_and_project(only=il.SUMMER_SLUGS)
    return jsonify({
        "live_html": render_template_string(il.LIVE_PARTIAL, games=live),
        "upcoming_html": render_template_string(il.UPCOMING_PARTIAL, upcoming=upcoming),
        "completed_html": render_template_string(il.COMPLETED_PARTIAL, completed=completed),
        "live_count": len(live), "upcoming_count": len(upcoming), "completed_count": len(completed),
        "updated_at": datetime.now(il.ET).strftime("%I:%M:%S %p ET"),
        "error": error,
    })


@app.route("/refresh")
def refresh_ratings():
    with il._ratings_thread_lock:
        il.RATINGS_LOADED_AT = None
        il._ratings_thread = None
    il.ensure_ratings_loading()
    return redirect(url_for("index"))


def main():
    ap = argparse.ArgumentParser(description="NBA Summer League Live Projections")
    ap.add_argument("--port", type=int, default=5015)
    ap.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = ap.parse_args()
    if args.date:
        il.DATE_OVERRIDE = args.date
    il.load_all_ratings()
    print("=" * 58)
    print("  NBA Summer League Live Projections")
    print("=" * 58)
    print(f"  Las Vegas + Salt Lake City -> http://localhost:{args.port}")
    print("  (reuses the International board's engine + self-computed ratings)")
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
