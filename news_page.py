#!/usr/bin/env python3
"""news_page.py - "Court & Cover" daily basketball + betting brief, as a web page.

Mounted at /news on the combined basketball site. Reuses the content engine from
basketball_gambling_newsletter.py (publisher RSS + YouTube + best-effort social)
and renders a dark, newspaper-style HTML page. Content is gathered server-side and
cached, so the page is always reasonably fresh without any client-side polling.
"""

from __future__ import annotations

import html
import threading
import time
from datetime import datetime

from flask import Flask, Response

from basketball_gambling_newsletter import ET_TZ, fmt_when, gather_content

app = Flask(__name__)

# ── Cached content (gathered server-side, refreshed lazily) ──
_TTL = 1800  # 30 minutes
_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def get_content() -> dict:
    """Return the newsletter payload, refreshing at most every _TTL seconds.

    On a fetch failure, the last good payload is kept rather than blanking out.
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _TTL:
        return _cache["data"]
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["ts"] < _TTL:
            return _cache["data"]
        try:
            _cache["data"] = gather_content()
            _cache["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            print("[news] gather failed:", e)
            if _cache["data"] is None:
                _cache["data"] = {
                    "edition_date": datetime.now(ET_TZ), "generated_at": datetime.now(ET_TZ),
                    "lead": None, "top_stories": [], "betting": [], "youtube": [], "social": [],
                }
    return _cache["data"]


# Warm the cache in the background so the first visitor isn't kept waiting.
threading.Thread(target=lambda: get_content(), name="news-warm", daemon=True).start()


# ── Rendering ──
CSS = """
:root{--bg:#0f1923;--card:#16202c;--border:#26323f;--text:#eef2f6;--muted:#8a98a8;
--navy:#16243f;--orange:#e8730c;--red:#c4302b;--purple:#6a4aa0;--link:#ffae6b}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Georgia,'Times New Roman',serif;background:var(--bg);color:var(--text);
line-height:1.5}
.mast{background:var(--navy);border-bottom:4px solid var(--orange);padding:22px 18px 16px}
.mast h1{font-family:-apple-system,'Segoe UI',sans-serif;font-size:2.4em;font-weight:800;
letter-spacing:.5px;color:#fff;line-height:1}
.mast .sub{font-family:-apple-system,'Segoe UI',sans-serif;color:#ffd9b3;font-size:.9em;
margin-top:7px}
.mast .menu{font-family:-apple-system,'Segoe UI',sans-serif;display:inline-block;margin-top:10px;
color:#cfe0ff;text-decoration:none;font-size:.82em;font-weight:600}
.mast .menu:hover{text-decoration:underline}
.wrap{max-width:880px;margin:0 auto;padding:22px 18px 10px}
.lead{border-bottom:2px solid var(--border);padding-bottom:18px;margin-bottom:8px}
.lead .lhl{font-family:-apple-system,'Segoe UI',sans-serif;font-size:1.7em;font-weight:800;
color:var(--text);text-decoration:none;display:block;line-height:1.2}
.lead .lhl:hover{color:var(--link)}
.lead .lsum{margin-top:8px;font-size:1.05em;color:#c4cdd8}
.sec{font-family:-apple-system,'Segoe UI',sans-serif;font-size:1.05em;font-weight:800;
color:#fff;letter-spacing:.5px;padding:7px 12px;margin:26px 0 14px;border-radius:4px}
.sec.navy{background:var(--navy)}.sec.orange{background:var(--orange)}
.sec.red{background:var(--red)}.sec.purple{background:var(--purple)}
.list{display:block}
.story{padding:0 0 13px;margin-bottom:13px;border-bottom:1px solid var(--border)}
.story .hl{font-family:-apple-system,'Segoe UI',sans-serif;font-size:1.12em;font-weight:700;
color:var(--text);text-decoration:none;display:block;line-height:1.3}
.story .hl:hover{color:var(--link)}
.by{font-family:-apple-system,'Segoe UI',sans-serif;color:var(--muted);font-size:.78em;
font-style:italic;margin:3px 0 5px}
.sum{color:#bcc6d1;font-size:.96em}
.disc{font-family:-apple-system,'Segoe UI',sans-serif;color:var(--muted);font-size:.78em;
margin:22px 0 6px;padding-top:14px;border-top:2px solid var(--orange);text-align:center}
.empty{color:var(--muted);font-style:italic;padding:30px 0}
@media(min-width:680px){.list{column-count:2;column-gap:26px}
.story{break-inside:avoid;-webkit-column-break-inside:avoid}}
"""


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _story(it: dict, summary: bool = True) -> str:
    src = it.get("source") or it.get("channel") or ""
    by = " &middot; ".join(_esc(x) for x in [src, fmt_when(it.get("published"))] if x)
    out = (f'<div class="story"><a class="hl" href="{_esc(it.get("url",""))}" '
           f'target="_blank" rel="noopener">{_esc(it.get("title",""))}</a>')
    if by:
        out += f'<div class="by">{by}</div>'
    if summary and it.get("summary"):
        out += f'<div class="sum">{_esc(it["summary"])}</div>'
    return out + "</div>"


def _section(label: str, cls: str, items: list, summary: bool = True) -> str:
    if not items:
        return ""
    body = "".join(_story(it, summary) for it in items)
    return f'<h2 class="sec {cls}">{label}</h2><div class="list">{body}</div>'


def render_page(data: dict) -> str:
    ed = data["edition_date"].strftime("%A, %B %d, %Y")
    gen = data["generated_at"].strftime(
        "%b %d, %#I:%M %p ET" if __import__("sys").platform == "win32" else "%b %d, %-I:%M %p ET")

    lead = data.get("lead")
    lead_html = ""
    if lead:
        by = " &middot; ".join(_esc(x) for x in [lead.get("source", ""),
                                                 fmt_when(lead.get("published"))] if x)
        lead_html = (f'<article class="lead"><a class="lhl" href="{_esc(lead.get("url",""))}" '
                     f'target="_blank" rel="noopener">{_esc(lead.get("title",""))}</a>'
                     f'<div class="by">{by}</div>')
        if lead.get("summary"):
            lead_html += f'<div class="lsum">{_esc(lead["summary"])}</div>'
        lead_html += "</article>"

    body = lead_html
    body += _section("TOP STORIES", "navy", data.get("top_stories", []))
    body += _section("THE BETTING BEAT", "orange", data.get("betting", []))
    body += _section("COURT-SIDE ON YOUTUBE", "red", data.get("youtube", []))
    if data.get("social"):
        body += _section("ON X &amp; INSTAGRAM", "purple", data["social"], summary=False)
    if not body:
        body = '<div class="empty">No fresh items right now — check back soon.</div>'

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Daily basketball &amp; sports-betting brief — top stories, the betting beat, and the latest from YouTube.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#16243f">
<title>Court &amp; Cover — Daily Basketball &amp; Betting Brief</title>
<style>{CSS}</style></head><body>
<header class="mast"><h1>COURT &amp; COVER</h1>
<div class="sub">Your daily brief on basketball &amp; betting &nbsp;|&nbsp; {_esc(ed)}</div>
<a class="menu" href="/">&#8962; Main Menu</a></header>
<div class="wrap">{body}
<div class="disc">Updated {_esc(gen)} &middot; Sources: publisher RSS, YouTube &middot;
For information only, not betting advice &middot; If gambling stops being fun, call 1-800-GAMBLER.</div>
</div></body></html>"""


@app.route("/")
def index():
    return Response(render_page(get_content()), mimetype="text/html")


if __name__ == "__main__":
    import os
    app.run(port=int(os.environ.get("PORT", 5009)), debug=False)
