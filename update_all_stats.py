#!/usr/bin/env python3
r"""update_all_stats.py - refresh every stat file used by the live & props projections.

For every sport, this refreshes the CSVs that the live-projection dashboards and
the props engines load from disk:

  NBA   nba_ratings_2026.csv      (Pace/OE/DE)  <- Basketball-Reference
  WNBA  wnba_ratings_2026.csv     (Pace/OE/DE)  <- Basketball-Reference (+ expansion)
  CBB   cbb_pace_ratings_2026.csv  (AdjPace)    <- WarrenNolan adjusted pace  (merge)
  WCBB  wcbb_pace_ratings_2026.csv (AdjPace)    <- WarrenNolan adjusted pace  (merge)

NBA/WNBA props read the same ratings CSVs as the live dashboards, so both are
covered by the two ratings files. CBB/WCBB offensive/defensive efficiency and the
Intl/NBL ratings are scraped live by their dashboards at runtime, so there is no
on-disk file to refresh for those.

The scraping logic is reused from the existing projection scripts (imported as
modules) so this stays in sync with how the dashboards read the data.

Run modes:
  --once             one update pass, then exit (this is what the scheduled task runs)
  --loop             run forever, updating every --interval-hours (default 24)
  --interval-hours N loop interval in hours (default 24)
  --install-task     register a Windows Scheduled Task that runs --once every day
  --uninstall-task   remove that scheduled task
  --time HH:MM       daily run time for --install-task (default 06:00)
  --status           print the last run summary and exit

Typical setup for "every 24 hours, automatically":
  py -3 update_all_stats.py --install-task            # daily at 06:00
  py -3 update_all_stats.py --install-task --time 04:30
"""

from __future__ import annotations

import argparse
import os
import csv
import importlib
import json
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Paths & constants ──

DOCS = Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
DOCS.mkdir(parents=True, exist_ok=True)
LOG_FILE = DOCS / "update_all_stats.log"
STATUS_FILE = DOCS / "update_all_stats_status.json"
SCRIPT_PATH = Path(__file__).resolve()
TASK_NAME = "UpdateSportsStats"
ET = ZoneInfo("America/New_York")

CBB_PACE_CSV = DOCS / "cbb_pace_ratings_2026.csv"
WCBB_PACE_CSV = DOCS / "wcbb_pace_ratings_2026.csv"
NBA_RATINGS_CSV = DOCS / "nba_ratings_2026.csv"
WNBA_RATINGS_CSV = DOCS / "wnba_ratings_2026.csv"

# WarrenNolan adjusted-pace pages (men's + women's)
CBB_PACE_URL = "https://www.warrennolan.com/basketball/2026/stats-adv-pace"
WCBB_PACE_URL = "https://www.warrennolan.com/basketballw/2026/stats-adv-pace"

# WarrenNolan-name -> ESPN-name aliases for men's pace (mirrors cbb_power_pace_daily.py).
# Used so alias rows in the CSV get their pace refreshed too.
CBB_ESPN_ALIASES = {
    "FAU": "Florida Atlantic",
    "Loyola-Chicago": "Loyola Chicago",
    "Loyola-Maryland": "Loyola Maryland",
    "UMass-Lowell": "UMass Lowell",
}

# Minimum team counts a scrape must return before we trust it enough to write.
# Guards against an empty/garbled response wiping a good CSV (e.g. out of season).
MIN_TEAMS = {
    "CBB": 200,
    "WCBB": 200,
    "NBA": 25,
    "WNBA": 10,
}


# ── Logging ──

def log(msg: str) -> None:
    """Print and append a timestamped line to the log file."""
    stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # never let logging failure abort an update


# ── Pace CSV merge (CBB / WCBB) ──

def merge_pace_csv(path: Path, scraped: dict[str, tuple[float, int]],
                   alias_to_wn: dict[str, str]) -> tuple[int, int, int]:
    """Merge freshly scraped pace into an existing pace CSV, non-destructively.

    Existing rows are updated in place by name (or via the alias map); rows that
    can't be matched to fresh data are kept as-is; teams new to WarrenNolan are
    appended. Ranks are recomputed by pace descending. Written atomically.

    Returns (rows_updated, rows_appended, total_rows).
    """
    rows: list[dict] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({"Team": row["Team"].strip(), "AdjPace": float(row["AdjPace"])})

    existing_names = {r["Team"] for r in rows}

    updated = 0
    for r in rows:
        wn = r["Team"] if r["Team"] in scraped else alias_to_wn.get(r["Team"])
        if wn and wn in scraped:
            new_pace = scraped[wn][0]
            if new_pace != r["AdjPace"]:
                r["AdjPace"] = new_pace
                updated += 1

    appended = 0
    for name, (pace, _rank) in scraped.items():
        if name not in existing_names:
            rows.append({"Team": name, "AdjPace": pace})
            existing_names.add(name)
            appended += 1

    rows.sort(key=lambda r: r["AdjPace"], reverse=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Team", "AdjPace"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r["Team"], r["AdjPace"]])
    tmp.replace(path)

    return updated, appended, len(rows)


# ── Ratings CSV refresh (NBA / WNBA) ──

def safe_save_ratings(sport: str, path: Path, save_fn, teams: list) -> None:
    """Back up the CSV, write via the module's save_fn, then validate.

    Restores the backup if the new file is missing/empty/short, so a bad scrape
    can never leave a broken ratings file behind.
    """
    min_rows = MIN_TEAMS[sport]
    if len(teams) < min_rows:
        raise RuntimeError(f"only {len(teams)} teams scraped (need >= {min_rows}); not writing")

    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)

    try:
        save_fn(teams)
        # Validate what we just wrote.
        with open(path, "r", newline="", encoding="utf-8") as f:
            written = list(csv.DictReader(f))
        if len(written) < min_rows:
            raise RuntimeError(f"wrote only {len(written)} rows (need >= {min_rows})")
    except Exception:
        if backup and backup.exists():
            shutil.copy2(backup, path)
            log(f"  [{sport}] write failed validation - restored backup")
        raise


# ── Per-sport update tasks ──

def update_nba() -> dict:
    nba = importlib.import_module("nba_live_projections")
    teams = nba.scrape_bref_ratings()
    safe_save_ratings("NBA", NBA_RATINGS_CSV, nba.save_ratings_csv, teams)
    return {"detail": f"{len(teams)} teams from Basketball-Reference", "count": len(teams)}


def update_wnba() -> dict:
    wnba = importlib.import_module("wnba_live_projections")
    teams = wnba.scrape_bref_ratings()
    # save_ratings_csv adds the 2026 expansion teams (Toronto, Portland) at league avg.
    safe_save_ratings("WNBA", WNBA_RATINGS_CSV, wnba.save_ratings_csv, teams)
    return {"detail": f"{len(teams)} teams from Basketball-Reference (+expansion)",
            "count": len(teams)}


def update_cbb_pace() -> dict:
    cbb = importlib.import_module("cbb_live_projections")
    scraped = cbb.scrape_wn_ratings(CBB_PACE_URL)
    if len(scraped) < MIN_TEAMS["CBB"]:
        raise RuntimeError(f"only {len(scraped)} pace teams scraped (need >= {MIN_TEAMS['CBB']})")
    alias_to_wn = {espn: wn for wn, espn in CBB_ESPN_ALIASES.items()}
    upd, app, total = merge_pace_csv(CBB_PACE_CSV, scraped, alias_to_wn)
    return {"detail": f"{len(scraped)} scraped -> {upd} updated, {app} added, {total} total rows",
            "count": total}


def update_wcbb_pace() -> dict:
    wcbb = importlib.import_module("wcbb_live_projections")
    scraped = wcbb.scrape_wn_ratings(WCBB_PACE_URL)
    if len(scraped) < MIN_TEAMS["WCBB"]:
        raise RuntimeError(f"only {len(scraped)} pace teams scraped (need >= {MIN_TEAMS['WCBB']})")
    upd, app, total = merge_pace_csv(WCBB_PACE_CSV, scraped, {})
    return {"detail": f"{len(scraped)} scraped -> {upd} updated, {app} added, {total} total rows",
            "count": total}


TASKS = [
    ("NBA",  "nba_ratings_2026.csv",       update_nba),
    ("WNBA", "wnba_ratings_2026.csv",      update_wnba),
    ("CBB",  "cbb_pace_ratings_2026.csv",  update_cbb_pace),
    ("WCBB", "wcbb_pace_ratings_2026.csv", update_wcbb_pace),
]


# ── Orchestration ──

def run_once() -> list[dict]:
    """Run every sport's update once. One failure never aborts the others."""
    log("=" * 64)
    log("Stat update pass starting")
    results = []
    for sport, fname, fn in TASKS:
        try:
            out = fn()
            log(f"  [OK]   {sport:5} {fname:28} {out['detail']}")
            results.append({"sport": sport, "file": fname, "status": "ok", **out})
        except Exception as e:  # noqa: BLE001 - isolate each sport
            log(f"  [FAIL] {sport:5} {fname:28} {e}")
            log("         " + traceback.format_exc().strip().replace("\n", "\n         "))
            results.append({"sport": sport, "file": fname, "status": "error",
                            "detail": str(e), "count": 0})

    ok = sum(1 for r in results if r["status"] == "ok")
    log(f"Pass complete: {ok}/{len(results)} sports updated")

    status = {
        "last_run": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "ok": ok,
        "total": len(results),
        "results": results,
    }
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass
    return results


def run_loop(interval_hours: float) -> None:
    log(f"Loop mode: updating every {interval_hours}h. Ctrl+C to stop.")
    while True:
        run_once()
        wake = datetime.now(ET).timestamp() + interval_hours * 3600
        log(f"Next update around {datetime.fromtimestamp(wake, ET).strftime('%Y-%m-%d %H:%M %Z')}")
        try:
            time.sleep(interval_hours * 3600)
        except KeyboardInterrupt:
            log("Loop stopped by user.")
            return


# ── Windows Task Scheduler integration ──

def install_task(time_str: str) -> int:
    """Register a daily Scheduled Task that runs this script with --once.

    Runs as the current user when logged on (no admin/elevation required). Uses
    pyw.exe (windowless launcher) if available so no console window flashes,
    otherwise falls back to py.exe.
    """
    launcher = "pyw" if shutil.which("pyw") else "py"
    tr = f'{launcher} -3 "{SCRIPT_PATH}" --once'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "DAILY",
           "/ST", time_str, "/TR", tr, "/F"]
    print("Registering scheduled task:")
    print("  " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        print(f"\nScheduled task '{TASK_NAME}' created: runs daily at {time_str}.")
        print(f"Command: {tr}")
        print(f"Remove it with:  py -3 \"{SCRIPT_PATH}\" --uninstall-task")
    else:
        print(f"\nFailed to create task (exit {proc.returncode}).")
    return proc.returncode


def uninstall_task() -> int:
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        print(f"Scheduled task '{TASK_NAME}' removed.")
    return proc.returncode


def show_status() -> None:
    if not STATUS_FILE.exists():
        print("No status yet - run an update first (py -3 update_all_stats.py --once).")
        return
    data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    print(f"Last run: {data['last_run']}  ({data['ok']}/{data['total']} sports OK)")
    print("-" * 64)
    for r in data["results"]:
        mark = "OK  " if r["status"] == "ok" else "FAIL"
        print(f"  [{mark}] {r['sport']:5} {r['file']:28} {r['detail']}")


# ── Entry point ──

def main() -> int:
    p = argparse.ArgumentParser(
        description="Refresh stat files used by the live & props projections.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true",
                   help="run one update pass and exit (default)")
    g.add_argument("--loop", action="store_true",
                   help="run forever, updating every --interval-hours")
    g.add_argument("--install-task", action="store_true",
                   help="register a daily Windows Scheduled Task")
    g.add_argument("--uninstall-task", action="store_true",
                   help="remove the scheduled task")
    g.add_argument("--status", action="store_true",
                   help="print the last run summary and exit")
    p.add_argument("--interval-hours", type=float, default=24.0,
                   help="loop interval in hours (default 24)")
    p.add_argument("--time", default="06:00",
                   help="HH:MM daily run time for --install-task (default 06:00)")
    args = p.parse_args()

    if args.install_task:
        return install_task(args.time)
    if args.uninstall_task:
        return uninstall_task()
    if args.status:
        show_status()
        return 0
    if args.loop:
        run_loop(args.interval_hours)
        return 0

    # default: --once
    results = run_once()
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
