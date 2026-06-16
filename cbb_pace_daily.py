"""
Daily College Basketball Pace Ratings Sheet Generator

Automatically fetches today's schedule from ESPN's public API,
loads pace data from WarrenNolan CSV, and generates a printable PDF.

Usage:
    py -3 cbb_pace_daily.py              # today's games
    py -3 cbb_pace_daily.py 2026-02-20   # specific date
"""

import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.request import urlopen, Request
import json

from reportlab.lib.pagesizes import letter
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

# ESPN location name -> WarrenNolan CSV name
# Only needed where ESPN's team.location differs from the CSV
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


def load_pace() -> tuple[dict[str, tuple[float, int]], int]:
    """Load pace ratings CSV. Returns (dict, total_teams)."""
    pace = {}
    if not PACE_CSV.exists():
        print(f"  WARNING: pace CSV not found at {PACE_CSV}. Continuing without pace.")
        return pace, 0
    with open(PACE_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pace[row["Team"].strip()] = (float(row["AdjPace"]), int(row["Rank"]))
    return pace, len(pace)


def resolve_team(espn_location: str, pace: dict) -> str:
    """Map ESPN location name to WarrenNolan CSV name."""
    if espn_location in pace:
        return espn_location
    mapped = ESPN_TO_WN.get(espn_location)
    if mapped and mapped in pace:
        return mapped
    return espn_location  # return as-is, will show N/A


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

        # Parse start time to ET
        utc_str = event["date"]  # e.g. "2026-02-17T00:00Z"
        utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        et_dt = utc_dt.astimezone(ET)
        time_str = et_dt.strftime("%-I:%M %p") if sys.platform != "win32" else et_dt.strftime("%#I:%M %p")
        sort_key = et_dt.hour * 100 + et_dt.minute

        away_name = away["team"].get("location", away["team"]["shortDisplayName"])
        home_name = home["team"].get("location", home["team"]["shortDisplayName"])

        games.append((time_str, sort_key, away_name, home_name))

    games.sort(key=lambda x: x[1])
    return games


def pctile(rank: int, total: int) -> int:
    return round((total - rank + 1) / total * 100)


def pctile_color(p: int):
    if p >= 90:
        return colors.HexColor("#c62828")
    elif p >= 75:
        return colors.HexColor("#ef6c00")
    elif p >= 50:
        return colors.HexColor("#f9a825")
    elif p >= 25:
        return colors.HexColor("#2e7d32")
    else:
        return colors.HexColor("#1565c0")


def build_pdf(games, pace, total_teams, target_date: datetime):
    day_name = target_date.strftime("%A")
    date_label = target_date.strftime(f"{day_name}, %B {target_date.day}, %Y")
    file_date = target_date.strftime(f"%b{target_date.day}_%Y")
    output_path = OUTPUT_DIR / f"CBB_Games_Pace_{file_date}.pdf"

    pagesize = letter

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=pagesize,
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.35 * inch,
        bottomMargin=0.35 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"], fontSize=14, spaceAfter=1,
    )
    subtitle_style = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=8,
        alignment=TA_CENTER, textColor=colors.grey, spaceAfter=4,
    )
    legend_style = ParagraphStyle(
        "Legend", parent=styles["Normal"], fontSize=7,
        textColor=colors.HexColor("#555555"),
    )

    elements = []
    elements.append(Paragraph(
        f"College Basketball Games &mdash; {date_label}", title_style
    ))
    elements.append(Paragraph(
        f"Pace = adjusted possessions per 40 min (WarrenNolan 2025-26) &bull; "
        f"Percentile among {total_teams:,} D1 teams (100% = fastest) &bull; "
        f"{len(games)} games",
        subtitle_style
    ))
    elements.append(Spacer(1, 4))

    header = ["Time\n(ET)", "Away Team", "Pace", "%ile", "Home Team", "Pace", "%ile"]
    data = [header]
    game_pace_data = []
    missing = set()

    for time_str, _, away, home in games:
        away_wn = resolve_team(away, pace)
        home_wn = resolve_team(home, pace)

        a_info = pace.get(away_wn)
        h_info = pace.get(home_wn)

        if a_info:
            a_pace, a_rank = a_info
            a_pct = pctile(a_rank, total_teams)
            a_pace_str = f"{a_pace:.1f}"
            a_pct_str = f"{a_pct}%"
        else:
            missing.add(away)
            a_rank = None
            a_pace_str = "N/A"
            a_pct_str = "N/A"

        if h_info:
            h_pace, h_rank = h_info
            h_pct = pctile(h_rank, total_teams)
            h_pace_str = f"{h_pace:.1f}"
            h_pct_str = f"{h_pct}%"
        else:
            missing.add(home)
            h_rank = None
            h_pace_str = "N/A"
            h_pct_str = "N/A"

        data.append([time_str, away, a_pace_str, a_pct_str, home, h_pace_str, h_pct_str])
        game_pace_data.append((a_rank, h_rank))

    if missing:
        print(f"WARNING: No pace data for: {sorted(missing)}")

    col_widths = [
        0.6 * inch, 1.55 * inch, 0.45 * inch, 0.45 * inch,
        1.55 * inch, 0.45 * inch, 0.45 * inch,
    ]

    table = Table(data, colWidths=col_widths, repeatRows=1)

    style_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (1, 1), (1, -1), "LEFT"),
        ("ALIGN", (4, 1), (4, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]


    table.setStyle(TableStyle(style_commands))
    elements.append(table)

    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        f"Source: WarrenNolan.com Adj. Pace (2025-26, {total_teams:,} D1 teams) &bull; "
        "Schedule: ESPN API",
        legend_style,
    ))

    doc.build(elements)
    return output_path


def main():
    # Parse date argument
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

    print(f"Found {len(games)} games. Loading pace data...")
    pace, total_teams = load_pace()

    print("Generating PDF...")
    output_path = build_pdf(games, pace, total_teams, target_date)

    print(f"PDF saved to: {output_path}")
    print(f"Total games: {len(games)}")


if __name__ == "__main__":
    main()
