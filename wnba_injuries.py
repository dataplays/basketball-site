#!/usr/bin/env python3
r"""wnba_injuries.py - basketball injury reports as a web page (mounted at /injuries).

One master list per league, merged across every free, scriptable source (no API
keys). A dropdown at the top selects the league:

  - WNBA  : Official (WNBA.com PDF) + ESPN + Action Network + Rotowire + Covers
  - NBA   : ESPN + Action Network + Rotowire + Covers
  - NCAA M: Action Network + Rotowire + Covers   (ESPN has no college injuries)

(Women's NCAA is intentionally omitted: no free source publishes that data.)

Each player appears once with a consolidated status badge and source-attribution
chips (OFF/ESPN/AN/RW/COV; hover a chip for that source's status). Gathered
server-side, cached ~10 min per league. Standalone: `py -3 wnba_injuries.py`
(serve) or `--once [--league nba]` (console).
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, request

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    ET = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
WNBA_REPORTS = "https://www.wnba.com/api/injury-reports"
WNBA_PAGE = "https://www.wnba.com/wnba-injury-report"

app = Flask(__name__)


def _http(url: str, timeout: int = 25, raw: bool = False, referer: str = ""):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else data.decode("utf-8", "replace")


# ── OFFICIAL WNBA report (PDF) ──

WNBA_TEAMS = {
    "Atlanta Dream", "Chicago Sky", "Connecticut Sun", "Dallas Wings",
    "Golden State Valkyries", "Indiana Fever", "Las Vegas Aces", "Los Angeles Sparks",
    "Minnesota Lynx", "New York Liberty", "Phoenix Mercury", "Portland Fire",
    "Seattle Storm", "Toronto Tempo", "Washington Mystics",
}
STATUSES = {"Out", "Questionable", "Doubtful", "Probable", "Available"}
_SKIP = {"Game Date", "Game Time", "Matchup", "Team", "Player Name",
         "Current Status", "Reason"}
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2} \(ET\)$")
_MATCHUP_RE = re.compile(r"^[A-Z]{2,3}@[A-Z]{2,3}$")
_NAME_RE = re.compile(r"^[A-Z][\w.'-]+,\s+\w")


def _parse_report_pdf(pdf_bytes: bytes) -> list[dict]:
    import fitz  # PyMuPDF (lazy import)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    lines: list[str] = []
    for page in doc:
        lines += [ln.strip() for ln in page.get_text().splitlines() if ln.strip()]
    rows: list[dict] = []
    cur = {"date": "", "time": "", "matchup": "", "team": ""}
    player: dict | None = None

    def flush():
        nonlocal player
        if player:
            rows.append(player)
            player = None

    for s in lines:
        if s.startswith("Injury Report:") or s.startswith("Page "):
            continue
        if s in _SKIP:
            continue
        if _DATE_RE.match(s):
            flush(); cur["date"] = s; continue
        if _TIME_RE.match(s):
            flush(); cur["time"] = s; continue
        if _MATCHUP_RE.match(s):
            flush(); cur["matchup"] = s; continue
        if s in WNBA_TEAMS:
            flush(); cur["team"] = s; continue
        if s in STATUSES:
            if player:
                player["status"] = s
            continue
        if _NAME_RE.match(s) and "Injury/Illness" not in s:
            flush()
            ln, fn = (s.split(",", 1) + [""])[:2]
            player = {**cur, "player": f"{fn.strip()} {ln.strip()}".strip(),
                      "status": "", "reason": ""}
            continue
        if player:
            player["reason"] = (player["reason"] + " " + s).strip()
    flush()
    for r in rows:
        r["reason"] = re.sub(r"\s+", " ", r["reason"]).replace("Injury/Illness - ", "").strip(" ;-")
    return rows


def fetch_official() -> dict:
    out = {"date_label": "", "report_label": "", "entries": [], "error": ""}
    try:
        meta = json.loads(_http(WNBA_REPORTS))
        out["date_label"] = meta.get("dateLabel", "")
        links = meta.get("links") or []
        if not links:
            return out
        latest = links[-1]
        out["report_label"] = latest.get("label", "")
        out["entries"] = _parse_report_pdf(_http(latest["href"], raw=True, timeout=30))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        print("[injuries] official failed:", out["error"], file=sys.stderr)
    return out


def _group(team_to_players: dict) -> list[dict]:
    out = [{"team": t, "players": ps} for t, ps in team_to_players.items()]
    out.sort(key=lambda g: g["team"])
    return out


# ── ESPN ──

def fetch_espn(path: str) -> list[dict]:
    try:
        data = json.loads(_http(f"{ESPN_BASE}/{path}/injuries"))
    except Exception as e:  # noqa: BLE001
        print(f"[injuries] ESPN {path} failed:", e, file=sys.stderr)
        return []
    tm: dict = {}
    for grp in data.get("injuries", []):
        team = grp.get("displayName", "?")
        for inj in grp.get("injuries", []):
            ath = inj.get("athlete") or {}
            tm.setdefault(team, []).append({
                "player": ath.get("displayName", "Unknown"),
                "pos": (ath.get("position") or {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "comment": (inj.get("shortComment") or inj.get("longComment") or "").strip(),
            })
    return _group(tm)


# ── Action Network ──

def fetch_actionnetwork(path: str) -> list[dict]:
    try:
        h = _http(f"https://www.actionnetwork.com/{path}/injury-report")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.DOTALL)
        nd = json.loads(m.group(1))
        injuries = nd["props"]["pageProps"]["injuries"]
    except Exception as e:  # noqa: BLE001
        print(f"[injuries] ActionNetwork {path} failed:", e, file=sys.stderr)
        return []
    tm: dict = {}
    for it in injuries or []:
        team = (it.get("team") or {}).get("full_name", "?")
        pl = it.get("player") or {}
        tm.setdefault(team, []).append({
            "player": pl.get("full_name") or f"{pl.get('first_name','')} {pl.get('last_name','')}".strip(),
            "pos": pl.get("position", ""),
            "status": it.get("status", ""),
            "comment": (it.get("comment") or it.get("description") or "").strip(),
        })
    return _group(tm)


# ── Covers (team name read from the page, so it works for any league) ──

def _cells(row_html: str) -> list[str]:
    cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)]
    return [c for c in cells if c]


def fetch_covers(path: str) -> list[dict]:
    try:
        c = _http(f"https://www.covers.com/sport/{path}/injuries")
    except Exception as e:  # noqa: BLE001
        print(f"[injuries] Covers {path} failed:", e, file=sys.stderr)
        return []
    # Map each collapse key -> full team name from the header ("City<br><span>Nick</span>").
    name_map: dict = {}
    for m in re.finditer(
            r"([A-Za-z][\w.&'/ -]*?)<br>\s*<span>([\w.&'/ -]+?)</span>.*?href=\"#injuryCollapse([A-Za-z0-9]{2,6})\"",
            c, re.DOTALL):
        name_map[m.group(3)] = re.sub(r"\s+", " ", f"{m.group(1).strip()} {m.group(2).strip()}").strip()
    out = []
    for m in re.finditer(r'id="injuryCollapse([A-Za-z0-9]{2,6})"(.*?)(<table[^>]*>.*?</table>)', c, re.DOTALL):
        team = name_map.get(m.group(1), m.group(1))
        players, cur = [], None
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(3), re.DOTALL):
            cells = _cells(row)
            if not cells or cells[0] == "Player":
                continue
            if len(cells) >= 3:
                stat = cells[2]
                cur = {"player": cells[0], "pos": cells[1],
                       "status": stat.split(" - ")[0].strip(),
                       "comment": (stat.split(" - ", 1)[1].strip() if " - " in stat else "")}
                players.append(cur)
            elif len(cells) == 1 and cur is not None and len(cells[0]) > 12:
                cur["comment"] = cells[0]
        if players:
            out.append({"team": team, "players": players})
    out.sort(key=lambda g: g["team"])
    return out


# ── Rotowire (JSON behind the JS table) ──

def fetch_rotowire(path: str) -> list[dict]:
    """Rotowire injuries from the JSON its JS table loads (needs a Referer)."""
    ref = f"https://www.rotowire.com/{path}/injury-report.php"
    url = f"https://www.rotowire.com/{path}/tables/injury-report.php?team=ALL&pos=ALL"
    try:
        data = json.loads(_http(url, referer=ref))
    except Exception as e:  # noqa: BLE001
        print(f"[injuries] Rotowire {path} failed:", e, file=sys.stderr)
        return []
    tm: dict = {}
    for rec in data:
        team = (rec.get("team") or "?").strip()
        injury = re.sub(r"<[^>]+>", "", str(rec.get("injury") or "")).strip()
        rdate = re.sub(r"<[^>]+>", "", str(rec.get("rDate") or "")).strip()
        comment = injury
        if rdate and "subscriber" not in rdate.lower() and rdate not in ("-", "N/A", "n/a"):
            comment = (comment + f" · est. return {rdate}").strip(" ·")
        tm.setdefault(team, []).append({
            "player": rec.get("player") or f"{rec.get('firstname','')} {rec.get('lastname','')}".strip(),
            "pos": rec.get("position", ""),
            "status": rec.get("status", ""),
            "comment": comment,
        })
    return _group(tm)


# ── League configs ──

_WNBA_KW = {
    "dream": "Atlanta Dream", "sky": "Chicago Sky", "sun": "Connecticut Sun",
    "wings": "Dallas Wings", "valkyr": "Golden State Valkyries", "fever": "Indiana Fever",
    "aces": "Las Vegas Aces", "spark": "Los Angeles Sparks", "lynx": "Minnesota Lynx",
    "liberty": "New York Liberty", "mercury": "Phoenix Mercury", "fire": "Portland Fire",
    "storm": "Seattle Storm", "tempo": "Toronto Tempo", "mystic": "Washington Mystics",
}
_WNBA_ABBR = {
    "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
    "DAL": "Dallas Wings", "GS": "Golden State Valkyries", "GSV": "Golden State Valkyries",
    "IND": "Indiana Fever", "LV": "Las Vegas Aces", "LVA": "Las Vegas Aces",
    "LA": "Los Angeles Sparks", "LAS": "Los Angeles Sparks", "MIN": "Minnesota Lynx",
    "NY": "New York Liberty", "NYL": "New York Liberty", "PHO": "Phoenix Mercury",
    "PHX": "Phoenix Mercury", "POR": "Portland Fire", "PRT": "Portland Fire",
    "SEA": "Seattle Storm", "TOR": "Toronto Tempo", "WAS": "Washington Mystics",
    "WSH": "Washington Mystics",
}

_NBA_LIST = [
    ("ATL", "Atlanta Hawks", "hawks"), ("BOS", "Boston Celtics", "celtics"),
    ("BKN", "Brooklyn Nets", "nets"), ("CHA", "Charlotte Hornets", "hornets"),
    ("CHI", "Chicago Bulls", "bulls"), ("CLE", "Cleveland Cavaliers", "cavaliers"),
    ("DAL", "Dallas Mavericks", "mavericks"), ("DEN", "Denver Nuggets", "nuggets"),
    ("DET", "Detroit Pistons", "pistons"), ("GSW", "Golden State Warriors", "warriors"),
    ("HOU", "Houston Rockets", "rockets"), ("IND", "Indiana Pacers", "pacers"),
    ("LAC", "LA Clippers", "clippers"), ("LAL", "Los Angeles Lakers", "lakers"),
    ("MEM", "Memphis Grizzlies", "grizzlies"), ("MIA", "Miami Heat", "heat"),
    ("MIL", "Milwaukee Bucks", "bucks"), ("MIN", "Minnesota Timberwolves", "timberwolves"),
    ("NOP", "New Orleans Pelicans", "pelicans"), ("NYK", "New York Knicks", "knicks"),
    ("OKC", "Oklahoma City Thunder", "thunder"), ("ORL", "Orlando Magic", "magic"),
    ("PHI", "Philadelphia 76ers", "76ers"), ("PHX", "Phoenix Suns", "suns"),
    ("POR", "Portland Trail Blazers", "blazers"), ("SAC", "Sacramento Kings", "kings"),
    ("SAS", "San Antonio Spurs", "spurs"), ("TOR", "Toronto Raptors", "raptors"),
    ("UTA", "Utah Jazz", "jazz"), ("WAS", "Washington Wizards", "wizards"),
]
_NBA_KW = {kw: full for _ab, full, kw in _NBA_LIST}
_NBA_ABBR = {ab: full for ab, full, _kw in _NBA_LIST}
_NBA_ABBR.update({"BRK": "Brooklyn Nets", "NO": "New Orleans Pelicans", "NOR": "New Orleans Pelicans",
                  "NY": "New York Knicks", "GS": "Golden State Warriors", "SA": "San Antonio Spurs",
                  "PHO": "Phoenix Suns", "WSH": "Washington Wizards", "UTAH": "Utah Jazz",
                  "CHO": "Charlotte Hornets"})

LEAGUES = {
    "wnba": {"label": "WNBA", "accent": "#e03e3e", "grouped": True, "match_team": True,
             "espn": "wnba", "an": "wnba", "covers": "basketball/wnba", "rw": "wnba",
             "official": True, "kw": _WNBA_KW, "abbr": _WNBA_ABBR},
    "nba": {"label": "NBA", "accent": "#1d8fe0", "grouped": True, "match_team": True,
            "espn": "nba", "an": "nba", "covers": "basketball/nba", "rw": "basketball",
            "official": False, "kw": _NBA_KW, "abbr": _NBA_ABBR},
    "ncaam": {"label": "NCAA Men", "accent": "#f08c00", "grouped": False, "match_team": False,
              "espn": None, "an": "ncaab", "covers": "basketball/ncaab", "rw": "cbasketball",
              "official": False, "kw": {}, "abbr": {}, "team_priority": ["RW", "AN", "COV"]},
}
DEFAULT_LEAGUE = "wnba"


# ── Combine + cache (per league) ──

def gather(league: str) -> dict:
    cfg = LEAGUES[league]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        if cfg["official"]:
            futs["OFF"] = ex.submit(fetch_official)
        if cfg["espn"]:
            futs["ESPN"] = ex.submit(fetch_espn, cfg["espn"])
        if cfg["an"]:
            futs["AN"] = ex.submit(fetch_actionnetwork, cfg["an"])
        if cfg["rw"]:
            futs["RW"] = ex.submit(fetch_rotowire, cfg["rw"])
        if cfg["covers"]:
            futs["COV"] = ex.submit(fetch_covers, cfg["covers"])
        res = {k: f.result() for k, f in futs.items()}
    official = res.get("OFF", {"date_label": "", "report_label": "", "entries": [], "error": ""})
    src_lists = {k: res[k] for k in ("ESPN", "AN", "RW", "COV") if k in res}
    now = datetime.now(ET) if ET else datetime.now()
    return {"league": league, "official": official, "src_lists": src_lists, "generated_at": now}


_TTL = 600
_caches: dict = {}        # league -> {"ts":..., "data":...}
_lock = threading.Lock()


def get_data(league: str) -> dict:
    now = time.time()
    c = _caches.setdefault(league, {"ts": 0.0, "data": None})
    if c["data"] is not None and now - c["ts"] < _TTL:
        return c["data"]
    with _lock:
        c = _caches[league]
        if c["data"] is not None and time.time() - c["ts"] < _TTL:
            return c["data"]
        try:
            c["data"] = gather(league)
            c["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            print("[injuries] gather failed:", e, file=sys.stderr)
            if c["data"] is None:
                c["data"] = {"league": league, "official": {"entries": [], "error": str(e)},
                             "src_lists": {}, "generated_at": datetime.now(ET) if ET else datetime.now()}
    return c["data"]


threading.Thread(target=lambda: get_data(DEFAULT_LEAGUE), name="inj-warm", daemon=True).start()


# ── Merge all sources into one master per-player list ──

SOURCE_ORDER = ["OFF", "ESPN", "AN", "RW", "COV"]
SOURCE_NAMES = {
    "OFF": "Official game-day report (WNBA.com)", "ESPN": "ESPN",
    "AN": "Action Network", "RW": "Rotowire", "COV": "Covers",
}
NAME_PRIORITY = ["ESPN", "AN", "OFF", "COV", "RW"]
_NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
_SEVERITY = [(("out", "season"), 5), (("doubt",), 4),
             (("quest", "game time", "gtd", "day"), 3), (("prob",), 2), (("avail",), 1)]


def _canon_team(s: str, cfg: dict) -> str:
    s = (s or "").strip()
    if not s:
        return "?"
    abbr = cfg.get("abbr") or {}
    if s.upper() in abbr:
        return abbr[s.upper()]
    low = s.lower()
    for kw, full in (cfg.get("kw") or {}).items():
        if kw in low:
            return full
    return s


def _canon_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _NAME_SUFFIX)


def _sev(status: str) -> int:
    s = (status or "").lower()
    for kws, rank in _SEVERITY:
        if any(k in s for k in kws):
            return rank
    return 0


def _pretty_status(status: str) -> str:
    s = (status or "").replace("_", " ").strip()
    return s.title() if s and s == s.lower() else s


def _add_player(records: dict, cfg: dict, src: str, team, name, pos, status, comment) -> None:
    cn = _canon_name(name)
    if not cn:
        return
    key = (_canon_team(team, cfg), cn) if cfg["match_team"] else cn
    rec = records.get(key)
    if rec is None:
        rec = {"cn": cn, "pos": "", "names": {}, "teams": {}, "src": {}}
        records[key] = rec
    rec["src"][src] = {"status": (status or "").strip(), "comment": (comment or "").strip()}
    rec["names"][src] = str(name).strip()
    if team:
        rec["teams"][src] = str(team).strip()
    if pos and not rec["pos"]:
        rec["pos"] = str(pos).strip()


def _display_name(rec: dict) -> str:
    for s in NAME_PRIORITY:
        if rec["names"].get(s):
            return rec["names"][s]
    return next(iter(rec["names"].values()), "Unknown")


def _display_team(rec: dict, cfg: dict) -> str:
    if cfg["match_team"]:
        for s in SOURCE_ORDER:
            if rec["teams"].get(s):
                return _canon_team(rec["teams"][s], cfg)
        return "?"
    for s in cfg.get("team_priority", ["AN", "COV", "RW", "ESPN", "OFF"]):
        if rec["teams"].get(s):
            return rec["teams"][s]
    return next(iter(rec["teams"].values()), "?")


def _consolidate_status(rec: dict) -> str:
    off = rec["src"].get("OFF", {}).get("status")
    if off:
        return _pretty_status(off)
    best, best_rank = "", -1
    for s in rec["src"].values():
        if s["status"] and _sev(s["status"]) > best_rank:
            best, best_rank = s["status"], _sev(s["status"])
    return _pretty_status(best)


def _best_comment(rec: dict) -> str:
    off = rec["src"].get("OFF", {}).get("comment")
    if off:
        return off
    cands = [s["comment"] for s in rec["src"].values() if s["comment"]]
    return max(cands, key=len) if cands else ""


def _merge_initials(records: dict, cfg: dict) -> None:
    """Fold abbreviated-first-name records ('B. Jones') into the full-name record
    on the same canonical team (same last name, first initial). Team-matched
    leagues only (needs a reliable team to scope the match)."""
    by_team: dict = {}
    for key, rec in records.items():
        by_team.setdefault(_display_team(rec, cfg), []).append((key, rec))
    drop = set()
    for recs in by_team.values():
        fulls = [r for _k, r in recs if len(r["cn"].split()[0]) > 1]
        for key, rec in recs:
            toks = rec["cn"].split()
            if len(toks) < 2 or len(toks[0]) != 1:
                continue
            initial, last = toks[0], toks[-1]
            target = next((f for f in fulls if f is not rec
                           and f["cn"].split()[-1] == last
                           and f["cn"].split()[0].startswith(initial)), None)
            if target is None:
                continue
            for s, info in rec["src"].items():
                target["src"].setdefault(s, info)
                target["names"].setdefault(s, rec["names"].get(s, ""))
                target["teams"].setdefault(s, rec["teams"].get(s, ""))
            if not target["pos"] and rec["pos"]:
                target["pos"] = rec["pos"]
            drop.add(key)
    for k in drop:
        records.pop(k, None)


def build_master(data: dict) -> list[dict]:
    """Merge official + every source list into one master list. Returns per-team
    groups (grouped leagues) or a single flat group with team-tagged rows."""
    cfg = LEAGUES[data["league"]]
    records: dict = {}
    for e in (data.get("official") or {}).get("entries", []) or []:
        _add_player(records, cfg, "OFF", e.get("team", ""), e.get("player", ""),
                    "", e.get("status", ""), e.get("reason", ""))
    for src, teams in (data.get("src_lists") or {}).items():
        for g in teams or []:
            for p in g.get("players", []):
                _add_player(records, cfg, src, g.get("team", ""), p.get("player", ""),
                            p.get("pos", ""), p.get("status", ""), p.get("comment", ""))
    if cfg["match_team"]:
        _merge_initials(records, cfg)
    for rec in records.values():
        rec["display"] = _display_name(rec)
        rec["team"] = _display_team(rec, cfg)
        rec["status"] = _consolidate_status(rec)
        rec["comment"] = _best_comment(rec)

    if cfg["grouped"]:
        teams_map: dict = {}
        for rec in records.values():
            teams_map.setdefault(rec["team"], []).append(rec)
        out = []
        for team in sorted(teams_map):
            players = sorted(teams_map[team], key=lambda r: (-_sev(r["status"]), r["display"].lower()))
            out.append({"team": team, "players": players})
        return out
    flat = sorted(records.values(),
                  key=lambda r: (r["team"].lower(), -_sev(r["status"]), r["display"].lower()))
    return [{"team": None, "players": flat}]


# ── Rendering ──

CSS = """
:root{--bg:#0f1923;--card:#16202c;--border:#26323f;--text:#eef2f6;--muted:#8a98a8;--accent:#e03e3e}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.45}
.mast{background:linear-gradient(180deg,#1a1320,#0f1923);border-bottom:4px solid var(--accent);padding:20px 18px 15px}
.mast h1{font-size:1.7em;font-weight:800}.mast h1 span{color:var(--accent)}
.mast .sub{color:var(--muted);font-size:.85em;margin-top:6px}
.mast .menu{display:inline-block;margin-top:9px;color:#cfe0ff;text-decoration:none;font-size:.82em;font-weight:600}
.lgwrap{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
.lgwrap label{color:var(--muted);font-size:.8em;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.lgsel{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:7px;
       padding:7px 12px;font-size:.95em;font-weight:700;cursor:pointer}
.lgsel:focus{outline:none;border-color:var(--accent)}
.wrap{max-width:1000px;margin:0 auto;padding:18px 16px 12px}
.sec{font-size:1.2em;font-weight:800;margin:16px 0 4px}
.sec small{font-weight:500;color:var(--muted);font-size:.66em}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;margin:9px 0;overflow:hidden}
.card .team{background:rgba(255,255,255,.04);padding:8px 14px;font-weight:700;font-size:.92em;border-bottom:1px solid var(--border)}
.row{display:flex;gap:10px;align-items:baseline;padding:7px 14px;border-bottom:1px solid rgba(255,255,255,.04);flex-wrap:wrap}
.row:last-child{border-bottom:none}.row .nm{font-weight:600;min-width:150px}
.row .tm{color:var(--muted);font-size:.82em;min-width:140px;font-weight:600}
.row .pos{color:var(--muted);font-size:.78em;min-width:24px}
.badge{font-size:.7em;font-weight:800;padding:2px 8px;border-radius:11px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap}
.b-out{background:rgba(224,70,62,.18);color:#ff7b73}
.b-doubtful{background:rgba(240,104,46,.18);color:#ff9b66}
.b-questionable{background:rgba(232,162,60,.18);color:#ffcd76}
.b-probable,.b-available{background:rgba(70,180,110,.18);color:#74d39a}
.b-default{background:rgba(138,152,168,.18);color:#b7c2cf}
.rsn{color:var(--muted);font-size:.85em;flex:1;min-width:160px}
.empty{color:var(--muted);font-style:italic;padding:14px 0}
.legend{display:flex;gap:8px 16px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:9px;padding:10px 14px;margin:8px 0 2px;font-size:.8em;color:var(--muted)}
.legend .lk{color:var(--text);font-weight:700}
.legend .note{flex-basis:100%;color:var(--muted);font-size:.92em}
.chips{display:inline-flex;gap:4px;flex-wrap:wrap}
.srcchip{font-size:.66em;font-weight:800;letter-spacing:.4px;padding:2px 6px;border-radius:5px;background:rgba(138,152,168,.16);color:#b7c2cf;border:1px solid var(--border);cursor:default}
.srcchip.s-off{background:rgba(224,70,62,.18);color:#ff7b73;border-color:rgba(224,70,62,.45)}
.varies{font-size:.72em;color:#ffcd76;cursor:default;white-space:nowrap}
.cnt{color:var(--muted);font-weight:500;font-size:.72em}
.team .cnt{font-size:.82em}
.disc{color:var(--muted);font-size:.78em;margin:22px 0 6px;padding-top:14px;border-top:2px solid var(--accent);text-align:center}
.disc a{color:#5aa0e0;text-decoration:none}
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _badge(status: str) -> str:
    s = (status or "").lower()
    if "doubt" in s:
        cls = "b-doubtful"
    elif "out" in s or "season" in s:
        cls = "b-out"
    elif "quest" in s or "game time" in s or "gtd" in s or "day" in s:
        cls = "b-questionable"
    elif "prob" in s or "avail" in s:
        cls = "b-probable"
    else:
        cls = "b-default"
    return f'<span class="badge {cls}">{_esc(status or "—")}</span>'


def _src_chips(rec: dict) -> str:
    out = []
    for s in SOURCE_ORDER:
        info = rec["src"].get(s)
        if not info:
            continue
        st = _pretty_status(info["status"]) or "listed"
        cls = "srcchip s-off" if s == "OFF" else "srcchip"
        out.append(f'<span class="{cls}" title="{_esc(SOURCE_NAMES[s])}: {_esc(st)}">{s}</span>')
    return '<span class="chips">' + "".join(out) + "</span>"


def _varies(rec: dict) -> str:
    buckets = {_sev(v["status"]) for v in rec["src"].values() if v["status"]}
    if len(buckets) <= 1:
        return ""
    detail = " · ".join(f"{s}: {_pretty_status(rec['src'][s]['status'])}" for s in SOURCE_ORDER
                        if s in rec["src"] and rec["src"][s]["status"])
    return f'<span class="varies" title="{_esc(detail)}">&#9888; varies</span>'


def _master_row(rec: dict, show_team: bool) -> str:
    tm = f'<span class="tm">{_esc(rec.get("team",""))}</span>' if show_team else ""
    pos = f'<span class="pos">{_esc(rec.get("pos",""))}</span>' if rec.get("pos") else ""
    return (f'<div class="row">{tm}<span class="nm">{_esc(rec["display"])}</span>{pos}'
            f'{_badge(rec["status"])}{_varies(rec)}{_src_chips(rec)}'
            f'<span class="rsn">{_esc(rec.get("comment","")) or "&mdash;"}</span></div>')


def render_page(data: dict, league: str) -> str:
    cfg = LEAGUES[league]
    gen = data["generated_at"]
    gen_str = gen.strftime("%b %d, %#I:%M %p ET" if sys.platform == "win32"
                           else "%b %d, %-I:%M %p ET")
    off = data.get("official") or {}
    groups = build_master(data)
    total = sum(len(g["players"]) for g in groups)

    src_keys = (["OFF"] if cfg["official"] else []) + \
               [k for k in ("ESPN", "AN", "RW", "COV") if k in (data.get("src_lists") or {})]
    legend_items = "".join(
        f'<span><span class="srcchip {"s-off" if k == "OFF" else ""}">{k}</span> '
        f'{_esc(SOURCE_NAMES[k].split(" (")[0])}</span>' for k in src_keys)
    legend = (f'<div class="legend"><span class="lk">Source key:</span>{legend_items}'
              '<span class="note">Each player appears once; chips show which sources list them '
              '(hover for that source&rsquo;s status). Badge = official status if on the game-day '
              'report, else the most-severe across sources. <span class="varies">&#9888; varies</span> '
              '= sources disagree (hover for the breakdown).</span></div>')

    ctx = f' &middot; official report: {_esc(off.get("date_label",""))}' if off.get("date_label") else ""
    body = [legend, f'<div class="sec">{_esc(cfg["label"])} Master Injury List '
            f'<small class="cnt">{total} players{ctx}</small></div>']

    if total:
        if cfg["grouped"]:
            for g in groups:
                body.append(f'<div class="card"><div class="team">{_esc(g["team"])} '
                            f'<span class="cnt">{len(g["players"])}</span></div>')
                body += [_master_row(r, show_team=False) for r in g["players"]]
                body.append("</div>")
        else:
            body.append('<div class="card">')
            body += [_master_row(r, show_team=True) for r in groups[0]["players"]]
            body.append("</div>")
    else:
        body.append('<div class="empty">No injuries available right now (sources temporarily unavailable).</div>')

    options = "".join(
        f'<option value="{k}"{" selected" if k == league else ""}>{_esc(v["label"])}</option>'
        for k, v in LEAGUES.items())

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Basketball injury reports — one master list per league (WNBA, NBA, NCAA Men) merging ESPN, Action Network, Rotowire, Covers and the official WNBA report, showing which sources list each player.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0f1923">
<title>{_esc(cfg["label"])} Injury Report</title><style>{CSS}</style>
<style>:root{{--accent:{cfg["accent"]}}}</style></head><body>
<header class="mast"><h1><span>&#9862;</span> Basketball Injury Report</h1>
<div class="sub">One master list per league across every free source &middot; updated {_esc(gen_str)}</div>
<div class="lgwrap"><label for="lg">League</label>
<select id="lg" class="lgsel" onchange="location.search='league='+this.value">{options}</select></div>
<a class="menu" href="/">&#8962; Main Menu</a></header>
<div class="wrap">{''.join(body)}
<div class="disc">Free sources: ESPN, Action Network, Rotowire, Covers
{"+ official WNBA.com" if cfg["official"] else ""} &middot; cached ~10 min &middot; for information only.</div>
</div></body></html>"""


@app.route("/")
def index():
    league = request.args.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    return Response(render_page(get_data(league), league), mimetype="text/html")


@app.route("/api/injuries")
def api_injuries():
    league = request.args.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    d = get_data(league)
    groups = build_master(d)
    return Response(json.dumps({
        "league": league,
        "generated_at": d["generated_at"].isoformat(),
        "players": [{
            "player": r["display"], "team": r.get("team", ""), "pos": r.get("pos", ""),
            "status": r["status"], "comment": r.get("comment", ""),
            "sources": {s: r["src"][s]["status"] for s in SOURCE_ORDER if s in r["src"]},
        } for g in groups for r in g["players"]],
    }, default=str), mimetype="application/json")


# ── CLI ──

def _print_console(league: str):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    d = gather(league)
    groups = build_master(d)
    total = sum(len(g["players"]) for g in groups)
    print(f"\n{LEAGUES[league]['label'].upper()} MASTER INJURY LIST - {total} players")
    print("Source key: " + " · ".join(f"{s}={SOURCE_NAMES[s].split(' (')[0]}" for s in SOURCE_ORDER))
    print("=" * 80)
    for g in groups:
        if g["team"]:
            print(f"\n{g['team']}")
        for r in g["players"]:
            srcs = ",".join(s for s in SOURCE_ORDER if s in r["src"])
            tm = f"{r['team'][:18]:18} " if g["team"] is None else "   "
            print(f"{tm}{r['display']:24} {r['status']:13} [{srcs:16}] {r.get('comment','')[:28]}")
    if not total:
        print("  (no injuries available)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Basketball injury reports (WNBA / NBA / NCAA Men)")
    ap.add_argument("--once", action="store_true", help="print to console and exit")
    ap.add_argument("--league", default=DEFAULT_LEAGUE, choices=list(LEAGUES))
    ap.add_argument("--port", type=int, default=5010)
    args = ap.parse_args()
    if args.once:
        _print_console(args.league)
        return 0
    print(f"Basketball injuries on http://localhost:{args.port}")
    app.run(port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
