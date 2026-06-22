# 🏀 Basketball Dashboards

Live basketball projections and sports-betting tools for the NBA, WNBA, college,
and international leagues — all served from one Flask app, updated automatically.

**Live site:** https://basketball-site-rva6.onrender.com/

> ⚠️ For information and entertainment only — **not betting advice**. If gambling
> stops being fun, call 1-800-GAMBLER.

---

## What's inside

A single Python/Flask application that mounts a dozen independent pages under one
address. Each dashboard pulls live data, projects games or players, and refreshes
on its own.

| Page | What it does |
|------|--------------|
| **`/nba`** | NBA live win/score projections — opponent-adjusted, clock-aware |
| **`/wnba`** | WNBA live projections |
| **`/cbb`** | Men's college basketball live projections |
| **`/wcbb`** | Women's college basketball live projections |
| **`/nbl`** | Australian NBL live projections |
| **`/intl`** | International — G League, EuroLeague/EuroCup, German BBL, and 16 domestic pro leagues (via api-sports) |
| **`/big3`** | BIG3 3-on-3 — a race-to-50 Monte-Carlo model (not clock-based) |
| **`/median`** | Median-probability calculator for player props |
| **`/news`** | *Court & Cover* — a daily basketball + betting news brief |
| **`/injuries`** | WNBA injury report aggregated from 5 sources (official WNBA.com + ESPN + Action Network + Rotowire + Covers) |
| **`/tools`** | Run the player-props projection & grading tools on demand |

The home page links to everything; a shared nav bar, footer, and "last updated"
indicator are injected into every dashboard automatically.

---

## How the projections work

- **Live game models** (`*_live_projections.py`): each team's expected scoring is
  opponent-adjusted — `proj = team_offense × opponent_defense / league_average` —
  combined with pace, then extrapolated using the **real game clock** (time
  elapsed vs. remaining in the period). Late-game blowouts regress the leading
  team's scoring rate toward league average. Neutral-site games drop home-court
  advantage automatically.
- **BIG3** (`big3_live_projections.py`): the league is a *race to 50, win by 2*
  (not a clock game), so it uses a **Monte-Carlo race simulation** that bakes the
  rules in directly (halftime at 25, cumulative score, win probability from the
  current score).
- **Player props** (`nba_props_projections.py`, `wnba_props_projections.py`):
  10,000-iteration Monte-Carlo simulations sampling each player's real
  game-to-game rates, with minutes, usage, pace, and matchup adjustments; reports
  edges and expected value vs. sportsbook lines.

Team ratings self-update from completed games where possible; ratings that can't
be derived live are read from CSVs in `data/`.

---

## Data sources

All free / public feeds — no scraping of paywalled data:

- **ESPN** APIs (scoreboards, box scores, injuries, player game logs)
- **Basketball-Reference** (NBA/WNBA team ratings)
- **WarrenNolan** (college pace & efficiency)
- **api-sports** (international domestic leagues — requires an `APISPORTS_KEY`)
- **EuroLeague** API, **WNBA.com** official injury report (PDF), **big3.com** feed
- Publisher **RSS** (CBS, Yahoo, Legal Sports Report) for the news brief

---

## Architecture

```
wsgi.py                      # entry point: landing page + dispatcher + scheduler
 ├─ DispatcherMiddleware     # mounts each app under its own /path
 ├─ _Injector (WSGI)         # injects shared nav/footer into every dashboard
 ├─ auto-discovery           # any *_live_projections.py exposing `app` is mounted
 ├─ EXTRA_DASHBOARDS         # median / news / injuries
 └─ daily scheduler          # rebuilds the CBB report PDFs at 06:00 ET
```

- Each dashboard is a self-contained Flask app exposing a module-level `app`.
- Mounted apps use **relative** fetch/asset URLs so they resolve under their mount path.
- Content is fetched server-side and cached (typically 20s–10min depending on the feed).

---

## Run it locally

```bash
pip install -r requirements.txt
python wsgi.py                      # serves http://localhost:8000
```

Useful environment variables:

| Variable | Purpose |
|----------|---------|
| `PORT` | Port to serve on (default 8000) |
| `APISPORTS_KEY` | Enables the api-sports international leagues on `/intl` |
| `CBB_REFRESH_HOUR` | Hour (0–23 ET) to auto-rebuild CBB reports (default 6) |
| `CBB_SCHEDULER_DISABLED=1` | Skip the daily scheduler (handy for local testing) |

Each dashboard can also be run standalone, e.g. `python big3_live_projections.py`.

---

## Deploy

Hosted on [Render](https://render.com) — auto-redeploys on every push to `main`
(~1–2 min build).

- **Build:** `pip install -r requirements.txt`
- **Start:** `gunicorn wsgi:application` (see `Procfile`)
- **Runtime:** see `runtime.txt`

A step-by-step hosting walkthrough (Render or Railway, custom domains, the daily
report scheduler) is in **[README_DEPLOY.md](README_DEPLOY.md)**.

---

## Tech stack

Python · Flask · Werkzeug (`DispatcherMiddleware`) · gunicorn · PyMuPDF (PDF
parsing) · reportlab (PDF reports) · standard-library HTTP/RSS/JSON parsing.

---

*Built for personal use. Live data belongs to its respective providers (ESPN,
WarrenNolan, Basketball-Reference, the WNBA, and others); this project just
aggregates and models it.*
