#!/usr/bin/env python3
r"""
prophetx_lines.py - Pull ProphetX exchange lines from the OddsPapi API (via
RapidAPI) and print every basketball market with BOTH the odds AND the amount
of money offered at each price (the exchange `limit`, in USD).

ProphetX is a peer-to-peer exchange, so each price has a real match size behind
it -- that's the `limit` field (the max USD you could take right now).

DATA FLOW (discovered live against the odds-api1 RapidAPI listing):
  /markets?sportId=11                       -> market-type catalog (names, lines)
  /fixtures?sportId=11&bookmakers=prophetx  -> games; keep those with hasOdds
  /fixtures/odds?fixtureId=ID&bookmakers=prophetx -> priced outcomes (price+limit)
  /players?playerIds=CSV                    -> player names for props

NOTE: OddsPapi sits behind Cloudflare, which 1010-blocks the default
Python-urllib User-Agent -- so a browser-like UA header is REQUIRED.

This module is import-friendly: gather() returns structured game dicts (used by
prophetx_live.py, the Flask dashboard). Catalog + player lookups are cached so
repeated calls stay cheap on API quota.

Usage (PowerShell):
    $env:ODDSPAPI_KEY = "your_rapidapi_key"
    py -3 prophetx_lines.py                      # all basketball ProphetX has
    py -3 prophetx_lines.py --tournament 486     # WNBA only  (NBA=132)
    py -3 prophetx_lines.py --min-limit 100      # hide prices with < $100 offered
    py -3 prophetx_lines.py --links              # include betslip deep links
    py -3 prophetx_lines.py --csv                # also write prophetx_lines.csv
    py -3 prophetx_lines.py --key <key>          # key inline instead of env var
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HOST = "odds-api1.p.rapidapi.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Ranking so markets print in a sensible order; player props last.
TYPE_RANK = {"moneyline": 0, "1x2": 1, "spreads": 2, "totals": 3,
             "teamtotals-team1": 4, "teamtotals-team2": 5}

# Module-level caches (benefit the dashboard's repeated polls).
_catalog_cache: dict = {}   # sport -> (ts, mkt_by_id, out_name)
_player_cache: dict = {}    # playerId -> name


def american_from_prob(p: float):
    """Fair American odds from a probability in (0,1); None if out of range."""
    if not (0.0 < p < 1.0):
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def fair_no_vig(d_a: float, l_a: float, d_b: float, l_b: float,
                kappa: float = 0.0, by_liability: bool = True):
    """Fair (de-vigged) and liquidity-shaded line for a 2-way exchange market.

    d_a/d_b: decimal price offered on each side. l_a/l_b: USD offered on each
    side. On a P2P exchange each side's offered money is posted by the OPPOSING
    side, so a big offer on A means capital is positioned on B -> the shade
    moves the fair line toward the side with LESS money offered. kappa is the
    shade strength in log-odds units (0 = pure no-vig); by_liability weights the
    offered stake by the maker's risk, stake*(decimal-1), before comparing.

    Returns a dict of fair/shaded probs + American odds for each side, plus the
    overround and the liquidity lean (toward A). None if the prices are invalid.
    """
    if not (d_a and d_b) or d_a <= 1 or d_b <= 1:
        return None
    qa, qb = 1.0 / d_a, 1.0 / d_b
    s = qa + qb
    if s <= 0:
        return None
    pa = qa / s                                   # de-vigged fair prob of A
    wa, wb = float(l_a or 0.0), float(l_b or 0.0)
    if by_liability:
        wa, wb = wa * (d_a - 1), wb * (d_b - 1)
    tot = wa + wb
    lean_a = (wb - wa) / tot if tot > 0 else 0.0  # >0 => shade toward A
    if 0.0 < pa < 1.0:
        za = math.log(pa / (1 - pa)) + kappa * lean_a
        pa_s = 1.0 / (1.0 + math.exp(-za))
    else:
        pa_s = pa
    return {
        "overround": round(s - 1, 4),
        "lean_a": round(lean_a, 4),
        "p_fair_a": pa, "p_fair_b": 1 - pa,
        "p_shaded_a": pa_s, "p_shaded_b": 1 - pa_s,
        "fair_a": american_from_prob(pa), "fair_b": american_from_prob(1 - pa),
        "shaded_a": american_from_prob(pa_s), "shaded_b": american_from_prob(1 - pa_s),
    }


# ── moneyline snapshot log (feeds prophetx_fair_backtest.py kappa calibration) ──
ML_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "prophetx_ml_log.csv")
ML_LOG_FIELDS = ["ts", "date", "book", "tournament", "away", "home",
                 "d_away", "l_away", "d_home", "l_home"]


def append_ml_snapshot(games: list, book: str = "prophetx", path: str = ML_LOG) -> int:
    """Append each game's 2-way moneyline (price + offered size per side) to the
    snapshot log. Returns rows written. Used to accumulate history for kappa
    calibration. NOTE: write target must be persistent (run locally; Render's
    filesystem is ephemeral)."""
    rows = []
    now = int(time.time())
    for g in games:
        ml = next((m for m in g["markets"]
                   if m.get("mtype") == "moneyline" and len(m["outcomes"]) == 2), None)
        if not ml:
            continue
        by_sel = {o["sel"]: o for o in ml["outcomes"]}
        a, h = by_sel.get(g["away"]), by_sel.get(g["home"])
        if not a or not h:
            continue
        ep = g.get("start_epoch")
        date = time.strftime("%Y%m%d", time.localtime(ep)) if ep else ""
        rows.append({"ts": now, "date": date, "book": book,
                     "tournament": g.get("tournament", ""),
                     "away": g["away"], "home": g["home"],
                     "d_away": a["decimal"], "l_away": a["limit"],
                     "d_home": h["decimal"], "l_home": h["limit"]})
    if not rows:
        return 0
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ML_LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def ml_log_stats(path: str = ML_LOG) -> dict:
    """Summary of the snapshot log: distinct games, snapshots, rows, last ts."""
    if not os.path.exists(path):
        return {"snapshots": 0, "games": 0, "rows": 0, "last": None}
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    snaps = {r["ts"] for r in rows}
    games = {(r["date"], r["away"], r["home"]) for r in rows}
    last = max((int(r["ts"]) for r in rows), default=None)
    return {"snapshots": len(snaps), "games": len(games),
            "rows": len(rows), "last": last}


class OddsPapiError(RuntimeError):
    pass


def _get(path: str, key: str, **params) -> object:
    """GET JSON from the OddsPapi RapidAPI proxy (with the required UA header)."""
    url = f"https://{HOST}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 403 and "not subscribed" in body.lower():
            raise OddsPapiError(
                "Key isn't subscribed to the OddsPapi (odds-api1) API. Subscribe "
                "at https://rapidapi.com/odds-papi-odds-papi-default/api/odds-api1/pricing"
            ) from exc
        raise OddsPapiError(f"HTTP {exc.code} on {path}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise OddsPapiError(f"Network error on {path}: {exc}") from exc


def get_catalog(key: str, sport: int, ttl: float = 3600.0):
    """marketId -> meta and outcomeId -> name, cached (catalog rarely changes)."""
    hit = _catalog_cache.get(sport)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], hit[2]
    catalog = _get("/markets", key, sportId=sport)
    mkt_by_id = {m["marketId"]: m for m in catalog}
    out_name = {o["outcomeId"]: o["outcomeName"]
                for m in catalog for o in m.get("outcomes", [])}
    _catalog_cache[sport] = (time.time(), mkt_by_id, out_name)
    return mkt_by_id, out_name


def resolve_players(key: str, pids) -> dict:
    """Batch-resolve playerId -> name, fetching only ids we haven't seen."""
    need = sorted(p for p in pids if p and p not in _player_cache)
    if need:
        for p in _get("/players", key, playerIds=",".join(map(str, need))):
            _player_cache[p["playerId"]] = p["playerName"]
    return _player_cache


def collect_outcomes(node: object, acc: list) -> None:
    """Recursively pull priced-outcome dicts out of the nested odds payload."""
    if isinstance(node, dict):
        if "price" in node and "marketId" in node:
            acc.append(node)
            return
        for value in node.values():
            collect_outcomes(value, acc)
    elif isinstance(node, list):
        for value in node:
            collect_outcomes(value, acc)


def stat_from_name(name: str) -> str:
    """'Over Under Player 3 Point FG (incl. overtime)' -> '3 Point FG'."""
    return (name.replace("Over Under Player ", "")
                .replace("Over Under ", "")
                .replace(" (incl. overtime)", "")
                .strip())


def market_family(meta: dict) -> str:
    """Period-bearing market name WITHOUT the line ('Spread', '1st Half
    Spread', 'Total Points', ...).

    Prefer the LONGER of marketName/marketNameShort: the short name collapses
    period markets — OddsPapi labels a full-game spread AND a 1st-half spread
    both 'Spread' — which silently merged different markets into one row.
    """
    short = (meta.get("marketNameShort") or "").strip()
    full = (meta.get("marketName") or "").strip()
    return (full if len(full) > len(short) else short) or meta.get("marketType", "")


def market_header(meta: dict, line: float, player: str) -> str:
    mtype = meta.get("marketType", "")
    name = market_family(meta)
    if meta.get("playerProp") and player:
        stat = stat_from_name(meta.get("marketName") or name)
        return f"{player} - {stat} {abs(line):g}"
    if mtype in ("moneyline", "1x2"):
        return name
    if "total" in mtype:
        return f"{name} {abs(line):g}"
    # Spreads carry the line in the header too. It used to live only in the
    # selection label, so every alt line shared the header "Spread".
    if "spread" in mtype or "handicap" in name.lower():
        return f"{name} {line:+g}" if line else name
    return f"{name} {line:+g}" if line else name


def selection_label(outcome: dict, meta: dict, out_name: dict,
                    teams: dict, line: float) -> str:
    name = out_name.get(outcome["outcomeId"], "?")
    label = teams.get(name, name)                       # "1"/"2" -> team name
    mtype = meta.get("marketType", "")
    if "spread" in mtype or "handicap" in (meta.get("marketNameShort") or "").lower():
        side = line if name == "1" else -line
        label = f"{label} {side:+g}"
    return label


def gather(key: str, sport: int = 11, tournament: int | None = None,
           book: str = "prophetx", min_limit: float = 0.0,
           catalog_ttl: float = 3600.0) -> list:
    """Return structured games with their priced markets (odds + limit)."""
    mkt_by_id, out_name = get_catalog(key, sport, catalog_ttl)

    fx_params = {"bookmakers": book}
    if tournament:
        fx_params["tournamentId"] = tournament
    else:
        fx_params["sportId"] = sport
    fixtures = _get("/fixtures", key, **fx_params)
    live = [g for g in fixtures
            if (g.get("bookmakers", {}).get(book) or {}).get("hasOdds")
            and not (g.get("bookmakers", {}).get(book) or {}).get("suspended")]
    if not live:
        return []

    def fetch_odds(g):
        return g, _get("/fixtures/odds", key, fixtureId=g["fixtureId"], bookmakers=book)
    with ThreadPoolExecutor(max_workers=8) as ex:
        loaded = list(ex.map(fetch_odds, live))

    pids = set()
    parsed = []
    for g, od in loaded:
        outs: list = []
        collect_outcomes(od.get("odds", {}), outs)
        parsed.append((g, outs))
        pids.update(o["playerId"] for o in outs if o.get("playerId"))
    players = resolve_players(key, pids)

    games = []
    for g, outs in parsed:
        p = g["participants"]
        teams = {"1": p["participant1Name"], "2": p["participant2Name"]}
        groups: dict = {}
        for o in outs:
            if (o.get("limit") or 0) < min_limit:
                continue
            groups.setdefault((o["marketId"], o.get("playerId") or 0), []).append(o)
        if not groups:
            continue

        ranked = []
        for (mid, pid), gouts in groups.items():
            meta = mkt_by_id.get(mid)
            if not meta:
                continue
            line = meta.get("handicap") or 0
            pname = players.get(pid, "")
            is_prop = bool(meta.get("playerProp"))
            rank = (1 if is_prop else 0, TYPE_RANK.get(meta.get("marketType"), 9),
                    pname, abs(line))
            outcomes = [{
                "sel": selection_label(o, meta, out_name, teams, line),
                "decimal": o["price"],
                "american": o.get("priceAmerican"),
                "limit": round(o.get("limit") or 0, 2),
                "betslip": o.get("betslip", ""),
                # OddsPapi's own ids — the ONLY reliable cross-book join. Two
                # prices sharing (market_id, outcome_id) are the same market,
                # same period, same line, by construction. Display strings are
                # not: 'Spread' collapses full-game and period markets.
                "outcome_id": o.get("outcomeId"),
            } for o in sorted(gouts, key=lambda o: -(o.get("limit") or 0))]
            ranked.append((rank, {
                "header": market_header(meta, line, pname),
                "is_prop": is_prop,
                "mtype": meta.get("marketType", ""),
                "market_id": mid,
                "family": market_family(meta),
                "player": pname,
                "line": line,
                "outcomes": outcomes,
            }))
        ranked.sort(key=lambda t: t[0])
        markets = [m for _, m in ranked]

        status = g.get("status", {})
        games.append({
            "fixture_id": g["fixtureId"],
            "away": p["participant2Name"], "home": p["participant1Name"],
            "game": f"{p['participant2Name']} @ {p['participant1Name']}",
            "tournament": g.get("tournament", {}).get("tournamentName", ""),
            "status": status.get("statusName", ""),
            "live": bool(status.get("live")),
            "start_epoch": g.get("startTime"),
            "markets": markets,
            "n_lines": sum(len(m["outcomes"]) for m in markets),
        })

    games.sort(key=lambda x: (not x["live"], x["start_epoch"] or 0))
    return games


# ── ProphetX OFFICIAL (direct) API feed ───────────────────────────────────────
# Same game-dict shape as gather(), but sourced from ProphetX's own Trading API
# (prophetx_api.py sibling module) instead of OddsPapi. Advantages: no RapidAPI
# quota, fresher prices, and FULL order-book depth per side (each outcome gains
# a `total` = money across all levels vs `limit` = top level only). Used by the
# dashboard's "PX Direct" view. NOTE: fixture ids here are ProphetX event ids
# (prefixed "px"), NOT OddsPapi fixture ids — so this slate must never be mixed
# into the Compare/Arbs joins, which key on OddsPapi ids.

import re as _re
import threading as _threading


class ProphetXDirectError(RuntimeError):
    """Direct ProphetX API feed unavailable (keys missing, auth, network)."""


# UI chip id (OddsPapi tournament id) -> official ProphetX tournament name(s)
DIRECT_CHIP_NAMES = {486: ["WNBA"], 132: ["NBA"], 15822: ["NBA Summer League"]}
# "All" = the core basketball set; resolved by exact name, missing ones skipped.
DIRECT_CORE = ["WNBA", "NBA", "NBA Summer League", "College Basketball",
               "Women's College Basketball", "BIG3", "EuroLeague",
               "Unrivaled", "EuroBasket"]

DIRECT_T_TTL = 3600.0    # tournament list cache (rarely changes)
DIRECT_EV_TTL = 300.0    # per-tournament event list cache (markets refresh often,
                         # event lists don't — keeps polls to 1 markets call/batch)

_direct: dict = {"client": None, "tournaments": (0.0, []), "events": {}}
_direct_lock = _threading.Lock()


def _direct_client():
    ak = os.environ.get("PROPHETX_ACCESS_KEY")
    sk = os.environ.get("PROPHETX_SECRET_KEY")
    if not ak or not sk:
        raise ProphetXDirectError(
            "PROPHETX_ACCESS_KEY / PROPHETX_SECRET_KEY not set on the server.")
    if _direct["client"] is None:
        try:
            from prophetx_api import ProphetXClient, PROD_BASE
        except ImportError as exc:
            raise ProphetXDirectError(f"prophetx_api.py not importable: {exc}")
        cli = ProphetXClient(PROD_BASE, ak, sk, verbose=False)
        try:
            cli.login()
        except Exception as exc:
            raise ProphetXDirectError(f"ProphetX login failed: {exc}")
        _direct["client"] = cli
    return _direct["client"]


def _direct_tournaments(cli):
    now = time.time()
    ts, lst = _direct["tournaments"]
    if not lst or now - ts > DIRECT_T_TTL:
        lst = cli.get_tournaments()
        _direct["tournaments"] = (now, lst)
    return lst


def _direct_events(cli, tid):
    now = time.time()
    hit = _direct["events"].get(tid)
    if hit and now - hit[0] < DIRECT_EV_TTL:
        return hit[1]
    evs = cli.get_sport_events(tid)
    _direct["events"][tid] = (now, evs)
    return evs


def _amer_to_dec(a):
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return round(1 + (a / 100.0 if a > 0 else 100.0 / -a), 3)


def _direct_betslip(line_id, display_odds):
    """Website auto-fill deep link (docs: auto-fill-integration-guide)."""
    if not line_id:
        return ""
    odds = f"&odds={display_odds.lstrip('+')}" if display_odds else ""
    return f"https://www.prophetx.co/?action=addtobetslip&line_id={line_id}{odds}"


def _direct_line_groups(m):
    """Yield selection groups: alt lines nest under market_lines; the parent
    may also carry its own selections (the main line)."""
    subs = m.get("market_lines") or []
    if subs:
        for s in subs:
            if s.get("selections"):
                yield s["selections"]
        if m.get("selections"):
            yield m["selections"]
    elif m.get("selections"):
        yield m["selections"]


def _direct_markets(mkts, min_limit=0.0):
    ranked = []
    for m in mkts or []:
        cat = m.get("category_name") or ""
        base = m.get("display_name") or m.get("name") or ""
        raw_type = m.get("type") or ""
        mtype = {"moneyline": "moneyline", "spread": "spreads",
                 "total": "totals"}.get(raw_type, raw_type)
        sub = m.get("sub_type") or ""
        is_prop = (cat not in ("Game Lines", "First Half")) or sub.startswith("player")
        player = _re.split(r"\s+Total\s+", base)[0].strip() if is_prop else ""
        cat_rank = 0 if cat == "Game Lines" else (1 if cat == "First Half" else 2)

        for sels in _direct_line_groups(m):
            outcomes = []
            line_val = 0.0
            for sel in sels:
                levels = sel if isinstance(sel, list) else [sel]
                levels = [lv for lv in levels if isinstance(lv, dict)]
                if not levels:
                    continue
                lv = levels[0]
                dec = _amer_to_dec(lv.get("odds"))
                if dec is None:
                    continue                       # empty rung: nothing offered
                stake = float(lv.get("stake") or 0)
                if stake < min_limit:
                    continue
                total = 0.0
                for l2 in levels:
                    try:
                        total += float(l2.get("stake") or 0)
                    except (TypeError, ValueError):
                        pass
                if lv.get("line") not in (None, 0):
                    try:
                        line_val = abs(float(lv["line"]))
                    except (TypeError, ValueError):
                        pass
                label = str(lv.get("name") or lv.get("display_name") or "?")
                outcomes.append({
                    "sel": label[:1].upper() + label[1:],
                    "decimal": dec,
                    "american": int(lv["odds"]) if isinstance(lv.get("odds"), (int, float)) else None,
                    "limit": round(stake, 2),
                    "total": round(total, 2),
                    "betslip": _direct_betslip(lv.get("line_id"), lv.get("display_odds") or ""),
                })
            if not outcomes:
                continue
            header = base if (mtype == "moneyline" or not line_val) else f"{base} {line_val:g}"
            rank = (1 if is_prop else 0, cat_rank,
                    TYPE_RANK.get(mtype, 9), player, line_val)
            ranked.append((rank, {
                "header": header, "is_prop": is_prop, "mtype": mtype,
                "player": player, "line": line_val, "outcomes": outcomes,
            }))
    ranked.sort(key=lambda t: t[0])
    return [mk for _, mk in ranked]


def _direct_game(ev, mkts, tname, min_limit):
    markets = _direct_markets(mkts, min_limit)
    if not markets:
        return None
    away = home = ""
    for c in ev.get("competitors") or []:
        side = c.get("side")
        nm = c.get("display_name") or c.get("name") or ""
        if side == "away":
            away = nm
        elif side == "home":
            home = nm
    if not (away and home):
        nm = ev.get("display_name") or ev.get("name") or ""
        if " at " in nm:
            away, home = nm.split(" at ", 1)
        else:
            away, home = nm, ""
    start_ms = None
    sched = ev.get("scheduled")
    if sched:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(str(sched).replace("Z", "+00:00"))
            start_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
    status = str(ev.get("status") or "")
    live = status.lower() in ("live", "in_progress", "inprogress")
    return {
        "fixture_id": f"px{ev.get('event_id')}",
        "away": away, "home": home,
        "game": f"{away} @ {home}" if home else away,
        "tournament": tname,
        "status": "Live" if live else (status.replace("_", " ").title() or "Upcoming"),
        "live": live,
        "start_epoch": start_ms,
        "markets": markets,
        "n_lines": sum(len(mk["outcomes"]) for mk in markets),
    }


def gather_direct(tournament: int = 0, min_limit: float = 0.0) -> list:
    """ProphetX official-API slate in gather()'s shape. `tournament` is the UI
    chip id (OddsPapi id: 486 WNBA / 132 NBA / 15822 Summer, 0 = all core
    basketball); it's mapped to official tournaments by name. Serialized by a
    lock: the client paces 1 req/s per endpoint (production base rate limit)."""
    with _direct_lock:
        cli = _direct_client()
        try:
            all_t = _direct_tournaments(cli)
        except ProphetXDirectError:
            raise
        except Exception as exc:
            raise ProphetXDirectError(f"tournament fetch failed: {exc}")
        by_name = {(t.get("name") or "").lower(): t for t in all_t}
        names = DIRECT_CHIP_NAMES.get(tournament, DIRECT_CORE) if tournament else DIRECT_CORE
        wanted = [by_name[n.lower()] for n in names if n.lower() in by_name]

        games = []
        for t in wanted:
            try:
                evs = _direct_events(cli, t["id"])
            except Exception:
                continue
            by_id = {e.get("event_id"): e for e in evs if e.get("event_id") is not None}
            ids = list(by_id)
            for i in range(0, len(ids), 50):
                chunk = ids[i:i + 50]
                try:
                    mm = cli.get_multiple_markets(chunk)
                except Exception:
                    continue
                for eid in chunk:
                    mkts = mm.get(str(eid)) or mm.get(eid) or []
                    if isinstance(mkts, dict):    # some responses wrap {markets: [...]}
                        mkts = mkts.get("markets") or []
                    g = _direct_game(by_id[eid], mkts, t.get("name", ""), min_limit)
                    if g:
                        games.append(g)
        games.sort(key=lambda x: (not x["live"], x["start_epoch"] or 0))
        return games


def to_console(games: list, links: bool = False) -> list:
    """Print games and return flat CSV rows."""
    rows = []
    total = 0
    for g in games:
        print(f"\n{'='*78}\n{g['game']}   [{g['tournament']} | {g['status']}]\n{'='*78}")
        for m in g["markets"]:
            print(f"  {m['header']}")
            for o in m["outcomes"]:
                am = f" ({o['american']:+d})" if isinstance(o["american"], int) else ""
                link = f"   {o['betslip']}" if links else ""
                print(f"      {o['sel']:<26} {o['decimal']:>6}{am:>8}   "
                      f"offered ${o['limit']:>9,.2f}{link}")
                total += 1
                rows.append({"game": g["game"], "tournament": g["tournament"],
                             "status": g["status"], "market": m["header"],
                             "selection": o["sel"], "decimal": o["decimal"],
                             "american": o["american"], "offered_usd": o["limit"],
                             "betslip": o["betslip"]})
    print(f"\n{total} priced outcomes across {len(games)} game(s).")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="ProphetX exchange lines via OddsPapi")
    ap.add_argument("--sport", type=int, default=11, help="OddsPapi sportId (11=basketball)")
    ap.add_argument("--tournament", type=int, help="limit to one tournament (WNBA=486, NBA=132)")
    ap.add_argument("--book", default="prophetx", help="bookmaker slug")
    ap.add_argument("--min-limit", type=float, default=0.0, help="hide prices offering < $N")
    ap.add_argument("--links", action="store_true", help="show betslip deep links")
    ap.add_argument("--csv", action="store_true", help="also write prophetx_lines.csv")
    ap.add_argument("--key", default=os.environ.get("ODDSPAPI_KEY", ""), help="RapidAPI key")
    args = ap.parse_args()
    if not args.key:
        sys.exit("No key. Set $env:ODDSPAPI_KEY or pass --key.")

    try:
        games = gather(args.key, sport=args.sport, tournament=args.tournament,
                       book=args.book, min_limit=args.min_limit)
    except OddsPapiError as exc:
        sys.exit(str(exc))

    if not games:
        print(f"No {args.book} basketball fixtures with odds right now.")
        return

    rows = to_console(games, links=args.links)

    if args.csv and rows:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prophetx_lines.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"CSV: {path}")


if __name__ == "__main__":
    main()
