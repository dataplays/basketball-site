#!/usr/bin/env python3
r"""wnba_injuries.py - WNBA injury report as a web page (mounted at /injuries).

Aggregates four free, scriptable sources (no API keys):
  - OFFICIAL (wnba.com): `/api/injury-reports` lists the league's game-day report
    PDFs (re-issued every 15 min); we parse the latest -> official designations
    (Out / Questionable / Doubtful / Probable / Available) for teams playing today.
  - ESPN: `site.api.espn.com/.../wnba/injuries` -> full league list (JSON).
  - ACTION NETWORK: `__NEXT_DATA__` on the injury page -> full league list (JSON).
  - COVERS: per-team HTML tables (injuryCollapse{ABBR}) -> full league list.

(Rotowire was requested too but renders its table via client-side JS with no
static feed, so it can't be read without a headless browser; left out for now.)

The official report is shown as the primary section; the three full-league
sources are collapsible. Gathered server-side and cached ~10 min. Standalone:
`py -3 wnba_injuries.py` (serve) or `--once` (console).
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    ET = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries"
ESPN_PAGE = "https://www.espn.com/wnba/injuries"
WNBA_REPORTS = "https://www.wnba.com/api/injury-reports"
WNBA_PAGE = "https://www.wnba.com/wnba-injury-report"
AN_PAGE = "https://www.actionnetwork.com/wnba/injury-report"
COVERS_PAGE = "https://www.covers.com/sport/basketball/wnba/injuries"
ROTOWIRE_PAGE = "https://www.rotowire.com/wnba/injury-report.php"
ROTOWIRE_INJ = "https://www.rotowire.com/wnba/tables/injury-report.php?team=ALL&pos=ALL"

app = Flask(__name__)


def _http(url: str, timeout: int = 25, raw: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else data.decode("utf-8", "replace")


def _group(team_to_players: dict) -> list[dict]:
    out = [{"team": t, "players": sorted(ps, key=lambda p: p["player"])}
           for t, ps in team_to_players.items()]
    out.sort(key=lambda g: g["team"])
    return out


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


# ── ESPN ──

def fetch_espn() -> list[dict]:
    try:
        data = json.loads(_http(ESPN_INJ))
    except Exception as e:  # noqa: BLE001
        print("[injuries] ESPN failed:", e, file=sys.stderr)
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

def fetch_actionnetwork() -> list[dict]:
    try:
        h = _http(AN_PAGE)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.DOTALL)
        nd = json.loads(m.group(1))
        injuries = nd["props"]["pageProps"]["injuries"]
    except Exception as e:  # noqa: BLE001
        print("[injuries] ActionNetwork failed:", e, file=sys.stderr)
        return []
    tm: dict = {}
    for it in injuries:
        team = (it.get("team") or {}).get("full_name", "?")
        pl = it.get("player") or {}
        tm.setdefault(team, []).append({
            "player": pl.get("full_name") or f"{pl.get('first_name','')} {pl.get('last_name','')}".strip(),
            "pos": pl.get("position", ""),
            "status": it.get("status", ""),
            "comment": (it.get("comment") or it.get("description") or "").strip(),
        })
    return _group(tm)


# ── Covers ──

COVERS_ABBR = {
    "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
    "DAL": "Dallas Wings", "GS": "Golden State Valkyries", "IND": "Indiana Fever",
    "LV": "Las Vegas Aces", "LA": "Los Angeles Sparks", "MIN": "Minnesota Lynx",
    "NY": "New York Liberty", "PHO": "Phoenix Mercury", "POR": "Portland Fire",
    "SEA": "Seattle Storm", "TOR": "Toronto Tempo", "WAS": "Washington Mystics",
}


def _cells(row_html: str) -> list[str]:
    cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)]
    return [c for c in cells if c]


def fetch_covers() -> list[dict]:
    try:
        c = _http(COVERS_PAGE)
    except Exception as e:  # noqa: BLE001
        print("[injuries] Covers failed:", e, file=sys.stderr)
        return []
    out = []
    for m in re.finditer(r'injuryCollapse([A-Z]{2,3})\b.*?(<table[^>]*>.*?</table>)', c, re.DOTALL):
        team = COVERS_ABBR.get(m.group(1), m.group(1))
        players, cur = [], None
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(2), re.DOTALL):
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
                cur["comment"] = cells[0]   # richer news blurb
        if players:
            out.append({"team": team, "players": players})
    out.sort(key=lambda g: g["team"])
    return out


# ── Rotowire (JSON behind the JS table) ──

ROTOWIRE_TEAMS = {
    "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
    "DAL": "Dallas Wings", "GS": "Golden State Valkyries", "GSV": "Golden State Valkyries",
    "IND": "Indiana Fever", "LV": "Las Vegas Aces", "LVA": "Las Vegas Aces",
    "LA": "Los Angeles Sparks", "LAS": "Los Angeles Sparks", "MIN": "Minnesota Lynx",
    "NY": "New York Liberty", "NYL": "New York Liberty", "PHO": "Phoenix Mercury",
    "PHX": "Phoenix Mercury", "POR": "Portland Fire", "PRT": "Portland Fire",
    "SEA": "Seattle Storm", "TOR": "Toronto Tempo", "WAS": "Washington Mystics",
    "WSH": "Washington Mystics",
}


def fetch_rotowire() -> list[dict]:
    """Rotowire injuries from the JSON endpoint its JS table loads (needs a Referer)."""
    try:
        req = urllib.request.Request(ROTOWIRE_INJ, headers={
            "User-Agent": UA, "Accept": "application/json", "Referer": ROTOWIRE_PAGE})
        data = json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        print("[injuries] Rotowire failed:", e, file=sys.stderr)
        return []
    tm: dict = {}
    for rec in data:
        team = ROTOWIRE_TEAMS.get((rec.get("team") or "").upper(), rec.get("team") or "?")
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


# ── Combine + cache ──

def gather() -> dict:
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_off = ex.submit(fetch_official)
        f_espn = ex.submit(fetch_espn)
        f_an = ex.submit(fetch_actionnetwork)
        f_cov = ex.submit(fetch_covers)
        f_rw = ex.submit(fetch_rotowire)
        official = f_off.result()
        sources = [
            ("ESPN", ESPN_PAGE, f_espn.result()),
            ("Action Network", AN_PAGE, f_an.result()),
            ("Rotowire", ROTOWIRE_PAGE, f_rw.result()),
            ("Covers", COVERS_PAGE, f_cov.result()),
        ]
    now = datetime.now(ET) if ET else datetime.now()
    return {"official": official, "sources": sources, "generated_at": now}


_TTL = 600
_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def get_data() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _TTL:
        return _cache["data"]
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["ts"] < _TTL:
            return _cache["data"]
        try:
            _cache["data"] = gather()
            _cache["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            print("[injuries] gather failed:", e, file=sys.stderr)
            if _cache["data"] is None:
                _cache["data"] = {"official": {"entries": [], "error": str(e)},
                                  "sources": [], "generated_at": datetime.now(ET) if ET else datetime.now()}
    return _cache["data"]


threading.Thread(target=lambda: get_data(), name="inj-warm", daemon=True).start()


# ── Rendering ──

CSS = """
:root{--bg:#0f1923;--card:#16202c;--border:#26323f;--text:#eef2f6;--muted:#8a98a8;--accent:#e03e3e}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.45}
.mast{background:linear-gradient(180deg,#1a1320,#0f1923);border-bottom:4px solid var(--accent);padding:20px 18px 15px}
.mast h1{font-size:1.7em;font-weight:800}.mast h1 span{color:var(--accent)}
.mast .sub{color:var(--muted);font-size:.85em;margin-top:6px}
.mast .menu{display:inline-block;margin-top:9px;color:#cfe0ff;text-decoration:none;font-size:.82em;font-weight:600}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px 12px}
.sec{font-size:1.2em;font-weight:800;margin:20px 0 4px}
.sec small{font-weight:500;color:var(--muted);font-size:.66em}
.game{margin:14px 0 6px;color:var(--accent);font-weight:700;font-size:.92em;letter-spacing:.5px}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;margin:9px 0;overflow:hidden}
.card .team{background:rgba(255,255,255,.04);padding:8px 14px;font-weight:700;font-size:.92em;border-bottom:1px solid var(--border)}
.row{display:flex;gap:10px;align-items:baseline;padding:7px 14px;border-bottom:1px solid rgba(255,255,255,.04);flex-wrap:wrap}
.row:last-child{border-bottom:none}.row .nm{font-weight:600;min-width:150px}
.row .pos{color:var(--muted);font-size:.78em;min-width:24px}
.badge{font-size:.7em;font-weight:800;padding:2px 8px;border-radius:11px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap}
.b-out{background:rgba(224,70,62,.18);color:#ff7b73}
.b-doubtful{background:rgba(240,104,46,.18);color:#ff9b66}
.b-questionable,.b-game-time-decision,.b-gtd{background:rgba(232,162,60,.18);color:#ffcd76}
.b-probable,.b-available{background:rgba(70,180,110,.18);color:#74d39a}
.b-day-to-day{background:rgba(232,162,60,.16);color:#ffcd76}
.b-default{background:rgba(138,152,168,.18);color:#b7c2cf}
.rsn{color:var(--muted);font-size:.85em;flex:1;min-width:160px}
.empty{color:var(--muted);font-style:italic;padding:14px 0}
details.src{margin:10px 0;border:1px solid var(--border);border-radius:9px;background:rgba(255,255,255,.015)}
details.src>summary{cursor:pointer;padding:11px 14px;font-weight:700;list-style:none}
details.src>summary::-webkit-details-marker{display:none}
details.src>summary:before{content:'\\25B8  ';color:var(--accent)}
details.src[open]>summary:before{content:'\\25BE  '}
details.src>summary small{font-weight:500;color:var(--muted);font-size:.8em}
details.src .inner{padding:0 12px 10px}
.disc{color:var(--muted);font-size:.78em;margin:22px 0 6px;padding-top:14px;border-top:2px solid var(--accent);text-align:center}
.disc a{color:#5aa0e0;text-decoration:none}
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _badge(status: str) -> str:
    s = (status or "").lower()
    if not s:
        cls = "b-default"
    elif "doubt" in s:
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


def _row(p: dict) -> str:
    pos = f'<span class="pos">{_esc(p.get("pos",""))}</span>' if p.get("pos") else ""
    return (f'<div class="row"><span class="nm">{_esc(p["player"])}</span>{pos}'
            f'{_badge(p.get("status",""))}<span class="rsn">{_esc(p.get("comment","")) or "&mdash;"}</span></div>')


def _source_block(name: str, url: str, teams: list, open_: bool) -> str:
    total = sum(len(t["players"]) for t in teams)
    if teams:
        inner = "".join(
            f'<div class="card"><div class="team">{_esc(g["team"])}</div>'
            + "".join(_row(p) for p in g["players"]) + "</div>" for g in teams)
    else:
        inner = '<div class="empty">Temporarily unavailable.</div>'
    return (f'<details class="src"{" open" if open_ else ""}><summary>{_esc(name)} '
            f'<small>&middot; {total} players &middot; '
            f'<a href="{_esc(url)}" target="_blank" rel="noopener" style="color:#5aa0e0">source</a></small></summary>'
            f'<div class="inner">{inner}</div></details>')


def render_page(data: dict) -> str:
    gen = data["generated_at"]
    gen_str = gen.strftime("%b %d, %#I:%M %p ET" if sys.platform == "win32"
                           else "%b %d, %-I:%M %p ET")
    off = data.get("official") or {}
    body = ['<div class="sec">Official Game-Day Report '
            f'<small>{_esc(off.get("date_label",""))} &middot; {_esc(off.get("report_label",""))}</small></div>']
    entries = off.get("entries") or []
    if entries:
        last_m = last_t = None
        opened = False
        for e in entries:
            if e["matchup"] != last_m:
                if opened:
                    body.append("</div>"); opened = False
                gt = e.get("time", "")
                body.append(f'<div class="game">{_esc(e["matchup"])}'
                            f'{("  &middot;  " + _esc(gt)) if gt else ""}</div>')
                last_m, last_t = e["matchup"], None
            if e["team"] != last_t:
                if opened:
                    body.append("</div>")
                body.append(f'<div class="card"><div class="team">{_esc(e["team"])}</div>')
                opened = True
                last_t = e["team"]
            body.append(
                f'<div class="row"><span class="nm">{_esc(e["player"])}</span>'
                f'{_badge(e["status"])}<span class="rsn">{_esc(e["reason"]) or "&mdash;"}</span></div>')
        if opened:
            body.append("</div>")
    elif off.get("error"):
        body.append('<div class="empty">Official report temporarily unavailable.</div>')
    else:
        body.append('<div class="empty">No game-day report entries (no games today, or not yet submitted).</div>')

    sources = data.get("sources") or []
    if sources:
        body.append('<div class="sec">Full League Injuries <small>by source &middot; click to expand</small></div>')
        for i, (name, url, teams) in enumerate(sources):
            body.append(_source_block(name, url, teams, open_=(i == 0)))

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="WNBA injury report — official game-day designations plus full league lists from ESPN, Action Network and Covers.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0f1923">
<title>WNBA Injury Report</title><style>{CSS}</style></head><body>
<header class="mast"><h1><span>&#9862;</span> WNBA Injury Report</h1>
<div class="sub">Official game-day report (WNBA.com) + ESPN, Action Network &amp; Covers &middot; updated {_esc(gen_str)}</div>
<a class="menu" href="/">&#8962; Main Menu</a></header>
<div class="wrap">{''.join(body)}
<div class="disc">Sources: official <a href="{WNBA_PAGE}" target="_blank" rel="noopener">WNBA.com</a>,
<a href="{ESPN_PAGE}" target="_blank" rel="noopener">ESPN</a>,
<a href="{AN_PAGE}" target="_blank" rel="noopener">Action Network</a>,
<a href="{ROTOWIRE_PAGE}" target="_blank" rel="noopener">Rotowire</a>,
<a href="{COVERS_PAGE}" target="_blank" rel="noopener">Covers</a> &middot; cached ~10 min &middot; for information only.</div>
</div></body></html>"""


@app.route("/")
def index():
    return Response(render_page(get_data()), mimetype="text/html")


@app.route("/api/injuries")
def api_injuries():
    d = get_data()
    return Response(json.dumps({
        "generated_at": d["generated_at"].isoformat(),
        "official": {k: v for k, v in (d.get("official") or {}).items() if k != "error"},
        "sources": {name: teams for name, _u, teams in (d.get("sources") or [])},
    }, default=str), mimetype="application/json")


# ── CLI ──

def _print_console():
    d = gather()
    off = d["official"]
    print(f"\nOFFICIAL WNBA REPORT - {off.get('date_label','')} ({off.get('report_label','')})")
    print("=" * 72)
    last = None
    for e in off.get("entries", []):
        tag = f"{e['matchup']} / {e['team']}"
        if tag != last:
            print(f"\n{tag}"); last = tag
        print(f"   {e['player']:24} {e['status']:13} {e['reason'][:38]}")
    if not off.get("entries"):
        print("  (no entries)", off.get("error", ""))
    for name, _url, teams in d["sources"]:
        n = sum(len(t["players"]) for t in teams)
        print(f"\n{name.upper()} - {n} players")
        print("=" * 72)
        for g in teams:
            print(f"  {g['team']}")
            for p in g["players"]:
                print(f"     {p['player']:24} {p.get('status',''):13} {p.get('comment','')[:40]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="WNBA injury report (official + ESPN + Action Network + Covers)")
    ap.add_argument("--once", action="store_true", help="print to console and exit")
    ap.add_argument("--port", type=int, default=5010)
    args = ap.parse_args()
    if args.once:
        _print_console()
        return 0
    print(f"WNBA injuries on http://localhost:{args.port}")
    app.run(port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
