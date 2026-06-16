"""
Daily College Basketball Power & Pace Sheet Generator

Automatically fetches today's schedule from ESPN's public API,
scrapes adjusted OE/DE ratings live from WarrenNolan.com (with Bart Torvik
fallback if WN is unreachable), loads pace data from CSV, and generates
a printable PDF.

Usage:
    py -3 cbb_power_pace_daily.py              # today's games
    py -3 cbb_power_pace_daily.py 2026-02-20   # specific date
"""

import csv
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

PACE_CSV = (Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data"))) / "cbb_pace_ratings_2026.csv")
OUTPUT_DIR = Path(os.environ.get("CBB_REPORT_DIR", str(Path(__file__).resolve().parent / "reports")))
OUTPUT_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

ESPN_API = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball/scoreboard"
)

WN_OE_URL = "https://www.warrennolan.com/basketball/2026/stats-adv-offensive-rating"
WN_DE_URL = "https://www.warrennolan.com/basketball/2026/stats-adv-defensive-rating"

# ESPN location name -> WarrenNolan name
ESPN_TO_WN = {
    "American University": "American",
    "App State": "Appalachian State",
    "Florida Gulf Coast": "FGCU",
    "Florida International": "FIU",
    "Grambling": "Grambling State",
    "Hawai'i": "Hawaii",
    "Kansas City": "UMKC",
    "Long Island University": "Long Island",
    "Loyola Marymount": "Loyola-Marymount",
    "Miami": "Miami (FL)",
    "NC State": "North Carolina State",
    "Pennsylvania": "Penn",
    "Presbyterian": "Presbyterian College",
    "Queens University": "Queens",
    "SE Louisiana": "Southeastern Louisiana",
    "SIU Edwardsville": "SIUE",
    "Saint Francis": "Saint Francis (PA)",
    "Saint Mary's": "Saint Mary's College",
    "Sam Houston": "Sam Houston State",
    "San José State": "San Jose State",
    "San Jose State": "San Jose State",
    "Southeast Missouri State": "Southeast Missouri",
    "St. Bonaventure": "Saint Bonaventure",
    "St. John's": "Saint John's",
    "St. Thomas-Minnesota": "Saint Thomas",
    "UAlbany": "Albany",
    "UConn": "Connecticut",
    "UL Monroe": "ULM",
    "UNC Greensboro": "UNCG",
    "UNC Wilmington": "UNCW",
    "UT Arlington": "UTA",
    "UT Martin": "Tennessee-Martin",
    "UT Rio Grande Valley": "UTRGV",
    "Detroit Mercy": "Detroit",
    "Little Rock": "Little Rock",
    "Purdue Fort Wayne": "Purdue Fort Wayne",
    "Seattle U": "Seattle University",
    "LIU": "Long Island",
    "USC Upstate": "South Carolina Upstate",
    "Bethune-Cookman": "Bethune-Cookman",
    "IU Indy": "IU Indianapolis",
    "Southern University": "Southern",
    "Massachusetts": "UMass",
}

# Bart Torvik name -> WarrenNolan name (for fallback data source)
TORVIK_TO_WN = {
    "Alabama St.": "Alabama State",
    "Alcorn St.": "Alcorn State",
    "Appalachian St.": "Appalachian State",
    "Arizona St.": "Arizona State",
    "Arkansas Pine Bluff": "Arkansas-Pine Bluff",
    "Arkansas St.": "Arkansas State",
    "Ball St.": "Ball State",
    "Bethune Cookman": "Bethune-Cookman",
    "Boise St.": "Boise State",
    "Cal Baptist": "California Baptist",
    "Cal St. Bakersfield": "Cal State Bakersfield",
    "Cal St. Fullerton": "Cal State Fullerton",
    "Cal St. Northridge": "Cal State Northridge",
    "Central Connecticut": "Central Connecticut",
    "Cleveland St.": "Cleveland State",
    "Colorado St.": "Colorado State",
    "Coppin St.": "Coppin State",
    "Delaware St.": "Delaware State",
    "Detroit Mercy": "Detroit",
    "East Tennessee St.": "East Tennessee State",
    "Florida Atlantic": "FAU",
    "Florida Gulf Coast": "FGCU",
    "Florida St.": "Florida State",
    "Fresno St.": "Fresno State",
    "Gardner Webb": "Gardner-Webb",
    "Georgia St.": "Georgia State",
    "Grambling St.": "Grambling State",
    "IU Indy": "IU Indianapolis",
    "Idaho St.": "Idaho State",
    "Illinois Chicago": "UIC",
    "Illinois St.": "Illinois State",
    "Indiana St.": "Indiana State",
    "Iowa St.": "Iowa State",
    "Jackson St.": "Jackson State",
    "Jacksonville St.": "Jacksonville State",
    "Kansas St.": "Kansas State",
    "Kennesaw St.": "Kennesaw State",
    "Kent St.": "Kent State",
    "Long Beach St.": "Long Beach State",
    "Louisiana Monroe": "ULM",
    "Loyola Chicago": "Loyola-Chicago",
    "Loyola MD": "Loyola-Maryland",
    "Loyola Marymount": "Loyola-Marymount",
    "McNeese St.": "McNeese",
    "Miami FL": "Miami (FL)",
    "Miami OH": "Miami (OH)",
    "Michigan St.": "Michigan State",
    "Mississippi": "Ole Miss",
    "Mississippi St.": "Mississippi State",
    "Mississippi Valley St.": "Mississippi Valley State",
    "Montana St.": "Montana State",
    "Morehead St.": "Morehead State",
    "Morgan St.": "Morgan State",
    "Mount St. Mary's": "Mount Saint Mary's",
    "Murray St.": "Murray State",
    "N.C. State": "North Carolina State",
    "Nebraska Omaha": "Omaha",
    "New Mexico St.": "New Mexico State",
    "Nicholls St.": "Nicholls",
    "Norfolk St.": "Norfolk State",
    "North Dakota St.": "North Dakota State",
    "Northwestern St.": "Northwestern State",
    "Ohio St.": "Ohio State",
    "Oklahoma St.": "Oklahoma State",
    "Oregon St.": "Oregon State",
    "Penn St.": "Penn State",
    "Portland St.": "Portland State",
    "Presbyterian": "Presbyterian College",
    "Sacramento St.": "Sacramento State",
    "Saint Francis": "Saint Francis (PA)",
    "Saint Mary's": "Saint Mary's College",
    "Sam Houston St.": "Sam Houston State",
    "San Diego St.": "San Diego State",
    "San Jose St.": "San Jose State",
    "Seattle": "Seattle University",
    "SIU Edwardsville": "SIUE",
    "South Carolina St.": "South Carolina State",
    "South Dakota St.": "South Dakota State",
    "Southeast Missouri St.": "Southeast Missouri",
    "Southeastern Louisiana": "Southeastern Louisiana",
    "St. Bonaventure": "Saint Bonaventure",
    "St. John's": "Saint John's",
    "St. Thomas": "Saint Thomas",
    "Tarleton St.": "Tarleton State",
    "Tennessee Martin": "Tennessee-Martin",
    "Tennessee St.": "Tennessee State",
    "Texas A&M Corpus Chris": "Texas A&M-Corpus Christi",
    "Texas St.": "Texas State",
    "UT Arlington": "UTA",
    "UT Rio Grande Valley": "UTRGV",
    "USC Upstate": "South Carolina Upstate",
    "Utah St.": "Utah State",
    "Washington St.": "Washington State",
    "Weber St.": "Weber State",
    "Wichita St.": "Wichita State",
    "Wright St.": "Wright State",
    "Youngstown St.": "Youngstown State",
}

TORVIK_URL = "https://barttorvik.com/2026_team_results.json"


def fetch_torvik_ratings() -> tuple[dict[str, tuple[float, int]], dict[str, tuple[float, int]], str]:
    """
    Fetch adjusted OE and DE from Bart Torvik's team JSON.
    Returns (oe_data, de_data, source_label).
    Torvik JSON columns: [0]=rank, [1]=team, [4]=adj_oe, [6]=adj_de
    """
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    r = requests.get(TORVIK_URL, headers=headers, timeout=20)
    r.raise_for_status()
    raw = json.loads(r.text)

    # Extract OE and DE, map Torvik names to WN names
    oe_list = []
    de_list = []
    for team in raw:
        name = TORVIK_TO_WN.get(team[1], team[1])
        oe = round(float(team[4]), 1)
        de = round(float(team[6]), 1)
        oe_list.append((name, oe))
        de_list.append((name, de))

    # Rank OE descending (higher is better offense)
    oe_list.sort(key=lambda x: x[1], reverse=True)
    oe_data = {}
    for rank, (name, val) in enumerate(oe_list, 1):
        oe_data[name] = (val, rank)

    # Rank DE ascending (lower is better defense)
    de_list.sort(key=lambda x: x[1])
    de_data = {}
    for rank, (name, val) in enumerate(de_list, 1):
        de_data[name] = (val, rank)

    # Also add ESPN alias names
    espn_aliases = {
        "FAU": "Florida Atlantic",
        "Loyola-Chicago": "Loyola Chicago",
        "Loyola-Maryland": "Loyola Maryland",
        "UMass-Lowell": "UMass Lowell",
    }
    for wn_name, espn_name in espn_aliases.items():
        if wn_name in oe_data:
            oe_data[espn_name] = oe_data[wn_name]
        if wn_name in de_data:
            de_data[espn_name] = de_data[wn_name]

    return oe_data, de_data, "Bart Torvik"


# ── Data loading ──

def load_pace() -> tuple[dict[str, tuple[float, int]], int]:
    """Load pace ratings CSV. Returns (dict, total_teams)."""
    pace = {}
    if not PACE_CSV.exists():
        print(f"  WARNING: pace CSV not found at {PACE_CSV}. "
              "Continuing without pace ratings.")
        return pace, 0
    with open(PACE_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pace[row["Team"].strip()] = (float(row["AdjPace"]), int(row["Rank"]))
    return pace, len(pace)


def scrape_wn_ratings(url: str) -> dict[str, tuple[float, int]]:
    """
    Scrape WarrenNolan advanced stats page.
    Returns dict of team_name -> (rating, rank).
    Works for both offensive and defensive rating pages.
    """
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    pattern = re.compile(
        r'<td class="data-cell data-center data-medium\s*"[^>]*>\s*(\d+)\s*</td>'
        r'.*?'
        r'<div class="name-subcontainer"><a[^>]*>([^<]+)</a></div>'
        r'.*?'
        r'<td class="data-cell data-center data-medium\s*"[^>]*>\s*([\d.]+)\s*</td>',
        re.DOTALL,
    )

    data = {}
    for m in pattern.finditer(html):
        rank = int(m.group(1))
        team = m.group(2).strip()
        rating = float(m.group(3))
        data[team] = (rating, rank)
    return data


def resolve_team(espn_location: str, pace: dict) -> str:
    """Map ESPN location name to WarrenNolan name."""
    if espn_location in pace:
        return espn_location
    mapped = ESPN_TO_WN.get(espn_location)
    if mapped and mapped in pace:
        return mapped
    return espn_location


def fetch_schedule(date_str: str) -> list[tuple[str, int, str, str]]:
    """
    Fetch all D1 games for a date from ESPN API.
    date_str: YYYYMMDD format
    Returns list of (time_str, sort_key, away_name, home_name).
    """
    url = f"{ESPN_API}?dates={date_str}&groups=50&limit=400"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        home = away = None
        for c in comp["competitors"]:
            if c["homeAway"] == "home":
                home = c
            else:
                away = c

        utc_str = event["date"]
        utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        et_dt = utc_dt.astimezone(ET)
        time_str = (
            et_dt.strftime("%-I:%M %p")
            if sys.platform != "win32"
            else et_dt.strftime("%#I:%M %p")
        )
        sort_key = et_dt.hour * 100 + et_dt.minute

        away_name = away["team"].get("location", away["team"]["shortDisplayName"])
        home_name = home["team"].get("location", home["team"]["shortDisplayName"])
        games.append((time_str, sort_key, away_name, home_name))

    games.sort(key=lambda x: x[1])
    return games


# ── Percentile helpers ──

def pctile(rank: int, total: int) -> int:
    return round((total - rank + 1) / total * 100)


def pctile_color(p: int):
    if p >= 90:
        return colors.HexColor("#c62828")   # dark red
    elif p >= 75:
        return colors.HexColor("#ef6c00")   # orange
    elif p >= 50:
        return colors.HexColor("#f9a825")   # amber
    elif p >= 25:
        return colors.HexColor("#2e7d32")   # green
    else:
        return colors.HexColor("#1565c0")   # blue


def _lookup(team_wn, data_dict):
    """Look up (rating_str, pctile_str, rank) for a team from a ratings dict."""
    info = data_dict.get(team_wn)
    if info:
        rating, rank = info
        total = len(data_dict)
        pct = pctile(rank, total)
        return f"{rating:.1f}", f"{pct}%", rank
    return "N/A", "N/A", None


# ── PDF generation ──

def build_pdf(games, pace, oe_data, de_data, target_date: datetime, source_label: str = "WarrenNolan"):
    total_pace = len(pace)
    total_oe = len(oe_data)
    total_de = len(de_data)

    day_name = target_date.strftime("%A")
    date_label = target_date.strftime(f"{day_name}, %B {target_date.day}, %Y")
    file_date = target_date.strftime(f"%b{target_date.day}_%Y")
    output_path = OUTPUT_DIR / f"CBB_Power_Pace_{file_date}.pdf"

    # Always landscape for the wider table
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(letter),
        leftMargin=0.30 * inch,
        rightMargin=0.30 * inch,
        topMargin=0.30 * inch,
        bottomMargin=0.30 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"], fontSize=13, spaceAfter=1,
    )
    subtitle_style = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=7,
        alignment=TA_CENTER, textColor=colors.grey, spaceAfter=3,
    )
    legend_style = ParagraphStyle(
        "Legend", parent=styles["Normal"], fontSize=6.5,
        textColor=colors.HexColor("#555555"),
    )

    elements = []
    elements.append(Paragraph(
        f"College Basketball Power &amp; Pace &mdash; {date_label}", title_style
    ))
    elements.append(Paragraph(
        f"OE/DE = adj. points per 100 poss. &bull; "
        f"Pace = adj. poss. per 40 min &bull; "
        f"Source: {source_label} 2025-26 ({total_oe} D1 teams) &bull; "
        f"{len(games)} games",
        subtitle_style,
    ))

    # Legend
    elements.append(Paragraph(
        "<b>%ile:</b> "
        "<font color='#c62828'>\u2588</font> 90th+ &nbsp; "
        "<font color='#ef6c00'>\u2588</font> 75-89th &nbsp; "
        "<font color='#f9a825'>\u2588</font> 50-74th &nbsp; "
        "<font color='#2e7d32'>\u2588</font> 25-49th &nbsp; "
        "<font color='#1565c0'>\u2588</font> &lt;25th &nbsp;&nbsp; | &nbsp;&nbsp; "
        "<b>OE:</b> higher = better offense &nbsp;&nbsp; "
        "<b>DE:</b> lower = better defense",
        legend_style,
    ))
    elements.append(Spacer(1, 3))

    # Table header
    # Columns: Time | Away | Pace | P% | OE | O% | DE | D% | Home | Pace | P% | OE | O% | DE | D%
    header = [
        "Time\n(ET)", "Away Team",
        "Pace", "P%", "OE", "O%", "DE", "D%",
        "Home Team",
        "Pace", "P%", "OE", "O%", "DE", "D%",
    ]
    data = [header]

    # Track ranks for coloring: list of dicts per row
    row_ranks = []
    missing = set()

    for time_str, _, away, home in games:
        away_wn = resolve_team(away, pace)
        home_wn = resolve_team(home, pace)

        # Away stats
        a_pace_s, a_ppct_s, a_pace_rank = _lookup(away_wn, pace)
        a_oe_s, a_opct_s, a_oe_rank = _lookup(away_wn, oe_data)
        a_de_s, a_dpct_s, a_de_rank = _lookup(away_wn, de_data)

        # Home stats
        h_pace_s, h_ppct_s, h_pace_rank = _lookup(home_wn, pace)
        h_oe_s, h_opct_s, h_oe_rank = _lookup(home_wn, oe_data)
        h_de_s, h_dpct_s, h_de_rank = _lookup(home_wn, de_data)

        if a_pace_rank is None:
            missing.add(away)
        if h_pace_rank is None:
            missing.add(home)

        row = [
            time_str, away,
            a_pace_s, a_ppct_s, a_oe_s, a_opct_s, a_de_s, a_dpct_s,
            home,
            h_pace_s, h_ppct_s, h_oe_s, h_opct_s, h_de_s, h_dpct_s,
        ]
        data.append(row)
        row_ranks.append({
            "a_pace": a_pace_rank, "a_oe": a_oe_rank, "a_de": a_de_rank,
            "h_pace": h_pace_rank, "h_oe": h_oe_rank, "h_de": h_de_rank,
        })

    if missing:
        print(f"WARNING: No data for: {sorted(missing)}")

    # Column widths (15 columns, landscape = ~10.4" usable)
    col_widths = [
        0.50 * inch,   # Time
        1.25 * inch,   # Away Team
        0.38 * inch,   # Pace
        0.33 * inch,   # P%
        0.38 * inch,   # OE
        0.33 * inch,   # O%
        0.38 * inch,   # DE
        0.33 * inch,   # D%
        1.25 * inch,   # Home Team
        0.38 * inch,   # Pace
        0.33 * inch,   # P%
        0.38 * inch,   # OE
        0.33 * inch,   # O%
        0.38 * inch,   # DE
        0.33 * inch,   # D%
    ]

    table = Table(data, colWidths=col_widths, repeatRows=1)

    style_commands = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 6),
        # Body
        ("FONTSIZE", (0, 1), (-1, -1), 6.5),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        # Alignment
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (1, 1), (1, -1), "LEFT"),   # Away Team left
        ("ALIGN", (8, 1), (8, -1), "LEFT"),   # Home Team left
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        # Grid & spacing
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        # Divider between away and home blocks
        ("LINEAFTER", (7, 0), (7, -1), 1.2, colors.HexColor("#1a1a2e")),
    ]

    # Color percentile cells
    # Columns with percentile data:
    #  3 = away P%,  5 = away O%,  7 = away D%
    # 10 = home P%, 12 = home O%, 14 = home D%
    pctile_cols = {
        "a_pace": (3, total_pace),
        "a_oe": (5, total_oe),
        "a_de": (7, total_de),
        "h_pace": (10, total_pace),
        "h_oe": (12, total_oe),
        "h_de": (14, total_de),
    }

    for i, ranks in enumerate(row_ranks, start=1):
        for key, (col, total) in pctile_cols.items():
            rank = ranks[key]
            if rank is not None:
                p = pctile(rank, total)
                style_commands.append(("BACKGROUND", (col, i), (col, i), pctile_color(p)))
                style_commands.append(("TEXTCOLOR", (col, i), (col, i), colors.white))
                style_commands.append(("FONTNAME", (col, i), (col, i), "Helvetica-Bold"))

    table.setStyle(TableStyle(style_commands))
    elements.append(table)

    # Footer
    elements.append(Spacer(1, 5))
    elements.append(Paragraph(
        f"Source: {source_label} Adj. OE, DE &amp; Pace (2025-26) &bull; "
        "Schedule: ESPN API",
        legend_style,
    ))

    doc.build(elements)
    return output_path


# ── Main ──

def main():
    if len(sys.argv) > 1:
        target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    else:
        target_date = datetime.now(ET)

    date_api = target_date.strftime("%Y%m%d")
    date_display = target_date.strftime(f"%B {target_date.day}, %Y")

    print(f"Fetching CBB schedule for {date_display}...")
    games = fetch_schedule(date_api)

    if not games:
        print("No games found for this date.")
        return

    print(f"Found {len(games)} games.")
    print("Loading pace data from CSV...")
    pace, _ = load_pace()

    source_label = "WarrenNolan"
    try:
        print("Scraping offensive ratings from WarrenNolan...")
        oe_data = scrape_wn_ratings(WN_OE_URL)
        print(f"  Got {len(oe_data)} teams.")

        print("Scraping defensive ratings from WarrenNolan...")
        de_data = scrape_wn_ratings(WN_DE_URL)
        print(f"  Got {len(de_data)} teams.")
    except (URLError, OSError, Exception) as e:
        print(f"  WarrenNolan unavailable: {e}")
        print("  Falling back to Bart Torvik for OE/DE data...")
        oe_data, de_data, source_label = fetch_torvik_ratings()
        print(f"  Got {len(oe_data)} OE and {len(de_data)} DE teams from Torvik.")

    print("Generating PDF...")
    output_path = build_pdf(games, pace, oe_data, de_data, target_date, source_label)

    print(f"PDF saved to: {output_path}")
    print(f"Total games: {len(games)}")


if __name__ == "__main__":
    main()
