#!/usr/bin/env python3
r"""basketball_gambling_newsletter.py - "Court & Cover" daily basketball + betting brief.

Aggregates fresh basketball & sports-betting content and renders a styled,
two-column NEWSPAPER-format PDF with readable story summaries (not just
headlines). Pure standard library + reportlab, no API keys, so it runs
unattended every morning.

Sources (all free, scriptable):
  - Stories : curated publisher RSS feeds (CBS, Yahoo, Legal Sports Report, ...)
              which carry real article summaries. Split into basketball news
              ("Top Stories") and the gambling angle ("The Betting Beat").
  - YouTube : curated betting/basketball channels via per-channel RSS feeds.
  - X / IG  : best-effort via site-restricted Google News RSS (usually empty -
              these platforms block automated access; rendered only if non-empty).

Output:
  Court_Cover_Brief_{MonDD}_{YYYY}.pdf

gather_content() returns a plain dict so the same payload can also feed a
website page (next on the roadmap).

Usage:
  py -3 basketball_gambling_newsletter.py            # build today's PDF
  py -3 basketball_gambling_newsletter.py --open     # build + open it
  py -3 basketball_gambling_newsletter.py --date 2026-06-21
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

# ── Paths & constants ──

DOCS = Path(os.environ.get("NEWSLETTER_DIR") or r"C:\Users\User\Documents")
CHANNEL_CACHE = DOCS / "newsletter_channels_cache.json"
SCRIPT_PATH = Path(__file__).resolve()
TASK_NAME = "CourtAndCoverNewsletter"
ET_TZ = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STORY_WINDOW_HOURS = 48
YT_WINDOW_HOURS = 60
SOCIAL_WINDOW_HOURS = 48

MAX_TOP = 14
MAX_BETTING = 10
MAX_YT = 10
MAX_SOCIAL = 6

# Publisher RSS feeds (carry summaries). kind: "hoops" = keep all (basketball
# sections); "betting" = multi-sport, filtered down to basketball items.
STORY_FEEDS = [
    ("CBS Sports — NBA",  "https://www.cbssports.com/rss/headlines/nba/",              "hoops"),
    ("CBS Sports — WNBA", "https://www.cbssports.com/rss/headlines/wnba/",             "hoops"),
    ("CBS Sports — CBB",  "https://www.cbssports.com/rss/headlines/college-basketball/", "hoops"),
    ("Yahoo Sports — NBA",  "https://sports.yahoo.com/nba/rss/",  "hoops"),
    ("Yahoo Sports — WNBA", "https://sports.yahoo.com/wnba/rss/", "hoops"),
    ("Legal Sports Report", "https://www.legalsportsreport.com/feed/", "betting"),
    ("Sports Handle",       "https://sportshandle.com/feed/",          "betting"),
    ("Gambling News",       "https://www.gamblingnews.com/feed/",      "betting"),
]

# Curated YouTube channels (display name, @handle, category).
CHANNELS = [
    ("The Action Network",      "actionnetworkhq",       "betting"),
    ("VSiN",                    "VSiNLive",              "betting"),
    ("Sports Gambling Podcast", "SportsGamblingPodcast", "betting"),
    ("WagerTalk TV",            "WagerTalkTV",           "betting"),
    ("OddsShark",               "oddsshark",             "betting"),
    ("NBA",                     "NBA",                   "basketball"),
    ("ESPN",                    "ESPN",                  "basketball"),
    ("Bleacher Report",         "bleacherreport",        "basketball"),
    ("House of Highlights",     "houseofhighlights",     "basketball"),
]

SEED_CHANNEL_IDS = {
    "actionnetworkhq":      "UCvv0ade-LVRA2fp9C5-C6hQ",
    "VSiNLive":             "UCTZVetvz6GVreC_N4fp92VA",
    "SportsGamblingPodcast": "UCqeSz-KlyY4v1-wRcZ1e7Yg",
    "NBA":                  "UCWJ2lWNubArHWmf3FIHbfcQ",
    "ESPN":                 "UC7i94bTxxuZBrllSxXHyFxg",
    "bleacherreport":       "UCO7BZhCe-EJxXIOU_O53n9g",
    "houseofhighlights":    "UC5qUhMoqke0mnJtgVoEn0aw",
}

SOCIAL_QUERIES = [
    'NBA betting (site:x.com OR site:twitter.com)',
    'WNBA betting (site:x.com OR site:twitter.com)',
    'basketball betting site:instagram.com',
]

# A story stays only if it reads as basketball (the betting angle is added by
# feed selection / BET_TERMS).
BBALL_TERMS = [
    "nba", "wnba", "basketball", "hoops", "ncaab", "ncaa tournament", "college hoops",
    "march madness", "g league", "g-league", "euroleague", "summer league", "big3",
    "celtics", "lakers", "warriors", "knicks", "nuggets", "thunder", "heat", "bucks",
    "draft", "liberty", "aces", "fever", "sparks", "mystics", "valkyries",
]

# Terms that flag a story for "The Betting Beat".
BET_TERMS = [
    "bet", "odds", "prop", "spread", "parlay", "sportsbook", "wager", "moneyline",
    "over/under", "favorite", "underdog", "draftkings", "fanduel", "betmgm", "caesars",
    "futures", "handicap", "against the spread", " ats", "pick", "cover", "line ",
]


# ── HTTP ──

def http_get(url: str, timeout: int = 22, retries: int = 2) -> str:
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last  # type: ignore[misc]


# ── Channel-id resolution (cached) ──

def load_channel_cache() -> dict:
    if CHANNEL_CACHE.exists():
        try:
            return json.loads(CHANNEL_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def resolve_channel_id(handle: str) -> str | None:
    try:
        html = http_get(f"https://www.youtube.com/@{handle}", timeout=18, retries=1)
    except Exception:  # noqa: BLE001
        return None
    for pat in (r'"channelId":"(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"',
                r'youtube\.com/channel/(UC[\w-]{22})'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def channel_ids() -> dict:
    cache = load_channel_cache()
    for h, cid in SEED_CHANNEL_IDS.items():
        cache.setdefault(h, cid)
    dirty = False
    for _name, handle, _cat in CHANNELS:
        if not cache.get(handle):
            cid = resolve_channel_id(handle)
            if cid:
                cache[handle] = cid
                dirty = True
    if dirty:
        try:
            CHANNEL_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except OSError:
            pass
    return cache


# ── Helpers ──

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def within(dt: datetime | None, hours: int, now: datetime) -> bool:
    return dt is not None and timedelta(0) <= (now - dt) <= timedelta(hours=hours)


def fmt_when(dt: datetime | None) -> str:
    if dt is None:
        return ""
    pat = "%b %d, %#I:%M %p ET" if sys.platform == "win32" else "%b %d, %-I:%M %p ET"
    return dt.astimezone(ET_TZ).strftime(pat)


def has_any(text: str, terms: list[str]) -> bool:
    t = (text or "").lower()
    return any(term in t for term in terms)


def clean_summary(raw: str | None, limit: int = 300) -> str:
    t = _html.unescape(raw or "")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # drop boilerplate tails
    t = re.split(r"(?i)\b(read more|continue reading|the post .* appeared first)\b", t)[0].strip()
    if len(t) > limit:
        cut = t[:limit]
        p = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[:p + 1] if p > limit * 0.5 else cut.rsplit(" ", 1)[0] + "…")
    return t


# ── Fetchers ──

def fetch_youtube(now: datetime) -> list[dict]:
    ids = channel_ids()
    meta = {h: (name, cat) for name, h, cat in CHANNELS}

    def one(handle: str) -> list[dict]:
        cid = ids.get(handle)
        if not cid:
            return []
        name, cat = meta[handle]
        try:
            xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
            root = ET.fromstring(xml)
        except Exception:  # noqa: BLE001
            return []
        ns = {"a": "http://www.w3.org/2005/Atom",
              "media": "http://search.yahoo.com/mrss/"}
        out = []
        for e in root.findall("a:entry", ns):
            title = _html.unescape((e.findtext("a:title", default="", namespaces=ns) or "").strip())
            link_el = e.find("a:link", ns)
            url = link_el.get("href") if link_el is not None else ""
            pub = parse_dt(e.findtext("a:published", default="", namespaces=ns))
            if not within(pub, YT_WINDOW_HOURS, now) or not has_any(title, BBALL_TERMS):
                continue
            mg = e.find("media:group/media:description", ns)
            out.append({"title": title, "url": url, "channel": name, "category": cat,
                        "published": pub, "summary": clean_summary(mg.text if mg is not None else "", 180)})
        return out[:3]

    vids: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(one, h) for _n, h, _c in CHANNELS]):
            vids.extend(f.result())
    vids.sort(key=lambda v: v["published"], reverse=True)
    return vids[:MAX_YT]


def _fetch_feed(source: str, url: str, kind: str) -> list[dict]:
    try:
        xml = http_get(url)
        root = ET.fromstring(xml)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in root.findall(".//item"):
        title = _html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        pub = parse_dt(it.findtext("pubDate"))
        desc = it.findtext("description") or it.findtext(
            "{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        out.append({"title": title, "url": link, "source": source, "kind": kind,
                    "published": pub, "summary": clean_summary(desc)})
    return out


def fetch_stories(now: datetime) -> tuple[list[dict], list[dict]]:
    """Return (top_stories, betting_beat), both with readable summaries."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda f: _fetch_feed(*f), STORY_FEEDS))

    seen: set[str] = set()
    top: list[dict] = []
    betting: list[dict] = []
    for items in results:
        for it in items:
            if not within(it["published"], STORY_WINDOW_HOURS, now):
                continue
            blob = f"{it['title']} {it['summary']}"
            # betting feeds must still be about basketball
            if it["kind"] == "betting" and not has_any(blob, BBALL_TERMS):
                continue
            # hoops feeds: title should look like basketball (drop stray cross-posts)
            if it["kind"] == "hoops" and not has_any(blob, BBALL_TERMS):
                continue
            key = re.sub(r"\W+", "", it["title"].lower())[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            is_bet = it["kind"] == "betting" or has_any(it["title"], BET_TERMS)
            (betting if is_bet else top).append(it)

    top.sort(key=lambda s: s["published"], reverse=True)
    betting.sort(key=lambda s: s["published"], reverse=True)
    return top[:MAX_TOP], betting[:MAX_BETTING]


def _google_news(query: str, when_days: int = 2) -> list[dict]:
    q = urllib.parse.quote(f"{query} when:{when_days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        root = ET.fromstring(http_get(url))
    except Exception:  # noqa: BLE001
        return []
    return [{"title": _html.unescape((it.findtext("title") or "").strip()),
             "url": (it.findtext("link") or "").strip(),
             "source": (it.find("source").text if it.find("source") is not None else ""),
             "published": parse_dt(it.findtext("pubDate"))}
            for it in root.findall(".//item")]


def fetch_social(now: datetime) -> list[dict]:
    seen: set[str] = set()
    posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda q: _google_news(q, 2), SOCIAL_QUERIES))
    for items in results:
        for it in items:
            url = it["url"].lower()
            platform = ("X" if ("x.com" in url or "twitter.com" in url)
                        else "Instagram" if "instagram.com" in url else None)
            if not platform or not within(it["published"], SOCIAL_WINDOW_HOURS, now):
                continue
            key = re.sub(r"\W+", "", it["title"].lower())[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            it["platform"] = platform
            posts.append(it)
    posts.sort(key=lambda s: s["published"], reverse=True)
    return posts[:MAX_SOCIAL]


def gather_content(date_str: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    print("  fetching publisher feeds ...", flush=True)
    top, betting = fetch_stories(now)
    print(f"    {len(top)} top stories, {len(betting)} betting stories")
    print("  fetching YouTube ...", flush=True)
    youtube = fetch_youtube(now)
    print(f"    {len(youtube)} videos")
    print("  fetching best-effort X / Instagram ...", flush=True)
    social = fetch_social(now)
    print(f"    {len(social)} social items")

    disp = (datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET_TZ)
            if date_str else datetime.now(ET_TZ))
    lead = top[0] if top else (betting[0] if betting else None)
    rest_top = top[1:] if (top and lead is top[0]) else top
    return {
        "generated_at": datetime.now(ET_TZ),
        "edition_date": disp,
        "lead": lead,
        "top_stories": rest_top,
        "betting": betting,
        "youtube": youtube,
        "social": social,
    }


# ── PDF (two-column newspaper) ──

NAVY = "#16243f"
ORANGE = "#e8730c"
YT_RED = "#c4302b"
PURPLE = "#5b3a8a"
INK = "#15171c"
GRAY = "#6b7280"
LINK = "#15396b"
RULE = "#cbd2dd"


def _esc(s: str) -> str:
    return xml_escape(s or "", {'"': "&quot;"})


def build_pdf(content: dict, path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (BaseDocTemplate, Frame, FrameBreak, HRFlowable,
                                    KeepTogether, NextPageTemplate, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle)

    PAGE_W, PAGE_H = letter
    LM = RM = 0.5 * inch
    TM = BM = 0.5 * inch
    GUT = 20
    usable = PAGE_W - LM - RM
    col_w = (usable - GUT) / 2
    top_h = 2.55 * inch

    # styles
    def ps(name, **kw):
        return ParagraphStyle(name, **kw)
    s_mast = ps("mast", fontName="Helvetica-Bold", fontSize=30, leading=32, textColor=colors.white)
    s_tag = ps("tag", fontName="Helvetica", fontSize=9.5, leading=12, textColor=colors.white)
    s_lead_h = ps("leadh", fontName="Helvetica-Bold", fontSize=17, leading=19.5, textColor=colors.HexColor(INK))
    s_lead_sum = ps("leads", fontName="Helvetica", fontSize=10, leading=13.5, textColor=colors.HexColor("#2c303a"))
    s_byline = ps("by", fontName="Helvetica-Oblique", fontSize=7.5, leading=9.5, textColor=colors.HexColor(GRAY),
                  spaceBefore=1, spaceAfter=3)
    s_head = ps("head", fontName="Helvetica-Bold", fontSize=10.5, leading=12.5, textColor=colors.HexColor(INK))
    s_sum = ps("sum", fontName="Helvetica", fontSize=8.7, leading=11, textColor=colors.HexColor("#33373f"),
               spaceBefore=1, spaceAfter=5)
    s_sec = ps("sec", fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=colors.white, leftIndent=5)

    def link_para(text, url, style, prefix=""):
        safe = _esc(text)
        inner = f'<a href="{_esc(url)}" color="{LINK}">{safe}</a>' if url else safe
        return Paragraph(prefix + inner, style)

    def section_bar(label, color, width):
        t = Table([[Paragraph(label, s_sec)]], colWidths=[width])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color)),
                               ("TOPPADDING", (0, 0), (-1, -1), 4),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    def rule():
        return HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(RULE),
                          spaceBefore=0, spaceAfter=5)

    def story_block(it, with_summary=True):
        parts = [link_para(it["title"], it.get("url", ""), s_head)]
        by = " · ".join(x for x in [it.get("source") or it.get("channel", ""),
                                          fmt_when(it.get("published"))] if x)
        parts.append(Paragraph(_esc(by), s_byline))
        if with_summary and it.get("summary"):
            parts.append(Paragraph(_esc(it["summary"]), s_sum))
        parts.append(rule())
        return KeepTogether(parts)

    flow: list = []

    # Masthead (full-width top frame)
    ed = content["edition_date"].strftime("%A, %B %d, %Y")
    mast = Table([[Paragraph('COURT &amp; COVER', s_mast)],
                  [Paragraph('Your daily brief on basketball &amp; betting&nbsp;&nbsp;|&nbsp;&nbsp;'
                             f'<font color="#ffd9b3">{_esc(ed)}</font>', s_tag)]],
                 colWidths=[usable])
    mast.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 12), ("BOTTOMPADDING", (0, 0), (0, 0), 1),
        ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 1), (-1, -1), 11),
        ("LINEBELOW", (0, -1), (-1, -1), 3, colors.HexColor(ORANGE)),
    ]))
    flow += [NextPageTemplate("later"), mast, Spacer(1, 9)]

    # Lead story (full width)
    lead = content["lead"]
    if lead:
        flow.append(link_para(lead["title"], lead.get("url", ""), s_lead_h))
        by = " · ".join(x for x in [lead.get("source", ""), fmt_when(lead.get("published"))] if x)
        flow.append(Paragraph(_esc(by), s_byline))
        if lead.get("summary"):
            flow.append(Paragraph(_esc(lead["summary"]), s_lead_sum))
    flow.append(FrameBreak())

    # Column body
    def add_section(label, color, items, with_summary=True):
        if not items:
            return
        blocks = [section_bar(label, color, col_w), Spacer(1, 5)]
        blocks.append(story_block(items[0], with_summary))
        flow.append(KeepTogether(blocks))
        for it in items[1:]:
            flow.append(story_block(it, with_summary))
        flow.append(Spacer(1, 6))

    add_section("TOP STORIES", NAVY, content["top_stories"])
    add_section("THE BETTING BEAT", ORANGE, content["betting"])
    add_section("COURT-SIDE ON YOUTUBE", YT_RED, content["youtube"])
    if content["social"]:
        add_section("ON X &amp; INSTAGRAM", PURPLE, content["social"], with_summary=False)

    if not (lead or content["top_stories"] or content["betting"]):
        flow.append(Paragraph("No fresh items in the last 48 hours — check back tomorrow.", s_sum))

    # footer drawn on every page
    gen = content["generated_at"].strftime(
        "%b %d, %Y %#I:%M %p ET" if sys.platform == "win32" else "%b %d, %Y %-I:%M %p ET")
    foot_txt = (f"Court & Cover  ·  Generated {gen}  ·  Sources: publisher RSS, YouTube  ·  "
                "For information only, not betting advice  ·  1-800-GAMBLER")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(ORANGE))
        canvas.setLineWidth(1)
        canvas.line(LM, 30, PAGE_W - RM, 30)
        canvas.setFont("Helvetica", 6.7)
        canvas.setFillColor(colors.HexColor(GRAY))
        canvas.drawString(LM, 21, foot_txt[:135])
        canvas.drawRightString(PAGE_W - RM, 21, f"p.{doc.page}")
        canvas.restoreState()

    col_top_y = PAGE_H - TM - top_h
    first_frames = [
        Frame(LM, col_top_y, usable, top_h, id="mast", leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0),
        Frame(LM, BM, col_w, col_top_y - BM - 6, id="c1", leftPadding=0, rightPadding=6, topPadding=0),
        Frame(LM + col_w + GUT, BM, col_w, col_top_y - BM - 6, id="c2", leftPadding=6, rightPadding=0, topPadding=0),
    ]
    later_frames = [
        Frame(LM, BM, col_w, PAGE_H - TM - BM, id="l1", leftPadding=0, rightPadding=6, topPadding=0),
        Frame(LM + col_w + GUT, BM, col_w, PAGE_H - TM - BM, id="l2", leftPadding=6, rightPadding=0, topPadding=0),
    ]
    doc = BaseDocTemplate(str(path), pagesize=letter,
                          title="Court & Cover - Daily Basketball & Betting Brief")
    doc.addPageTemplates([
        PageTemplate(id="first", frames=first_frames, onPage=footer),
        PageTemplate(id="later", frames=later_frames, onPage=footer),
    ])
    doc.build(flow)


# ── Windows Task Scheduler integration ──

def install_task(time_str: str) -> int:
    """Register a daily Scheduled Task that builds the brief each morning."""
    launcher = "pyw" if shutil.which("pyw") else "py"
    tr = f'{launcher} -3 "{SCRIPT_PATH}"'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "DAILY",
           "/ST", time_str, "/TR", tr, "/F"]
    print("Registering scheduled task:\n  " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        print(f"\nScheduled task '{TASK_NAME}' created: builds the brief daily at {time_str}.")
        print(f"Remove it with:  py -3 \"{SCRIPT_PATH}\" --uninstall-task")
    else:
        print(f"\nFailed to create task (exit {proc.returncode}).")
    return proc.returncode


def uninstall_task() -> int:
    proc = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        print(f"Scheduled task '{TASK_NAME}' removed.")
    return proc.returncode


# ── Entry point ──

def main() -> int:
    ap = argparse.ArgumentParser(description="Court & Cover - daily basketball + betting brief")
    ap.add_argument("--date", help="edition date YYYY-MM-DD (cosmetic; feeds are live)")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    ap.add_argument("--install-task", action="store_true",
                    help="register a daily Windows Scheduled Task")
    ap.add_argument("--uninstall-task", action="store_true", help="remove that scheduled task")
    ap.add_argument("--time", default="07:00", help="HH:MM daily run time for --install-task")
    args = ap.parse_args()

    if args.install_task:
        return install_task(args.time)
    if args.uninstall_task:
        return uninstall_task()

    print("Court & Cover - gathering content ...")
    content = gather_content(args.date)
    disp = content["edition_date"]
    out = DOCS / f"Court_Cover_Brief_{disp.strftime('%b%d_%Y')}.pdf"
    build_pdf(content, out)

    n = (1 if content["lead"] else 0) + len(content["top_stories"]) + len(content["betting"])
    print(f"\n  PDF saved to: {out}")
    print(f"  Stories: {n}  |  YouTube: {len(content['youtube'])}  |  Social: {len(content['social'])}")
    if args.open:
        import os
        os.startfile(str(out))  # noqa: S606
    return 0


if __name__ == "__main__":
    sys.exit(main())
