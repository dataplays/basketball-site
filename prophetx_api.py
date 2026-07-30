"""
ProphetX OFFICIAL Trading API client — read-only market data puller.

Pulls tournaments, events, markets, prices, and LIQUIDITY (money offered per
side) directly from ProphetX's own API. This is the direct feed that replaces/
augments the OddsPapi (RapidAPI) path in prophetx_lines.py — full order-book
depth per selection instead of top-of-book only.

READ-ONLY BY DESIGN: no order endpoints are implemented anywhere in this file.
It cannot place, cancel, or modify a wager. Safe to run at any time.

Environments:
  production  https://cash.api.prophetx.co/partner        (default)
  sandbox     https://api-ss-sandbox.betprophet.co/partner  (--sandbox)

Keys (env-only, NEVER commit):
  Production: $env:PROPHETX_ACCESS_KEY / $env:PROPHETX_SECRET_KEY
  Sandbox:    $env:PROPHETX_SB_ACCESS_KEY / $env:PROPHETX_SB_SECRET_KEY
              (--sandbox falls back to the production vars if SB ones unset)
  Generate: log into the ProphetX site (prod or sandbox) -> Menu -> API
  Integration -> Generate New Token -> copy access_key + secret_key.

Usage:
  py -3 prophetx_api.py                          # ALL BASKETBALL markets + liquidity (default)
  py -3 prophetx_api.py --list                   # login check + balance + tournament list only
  py -3 prophetx_api.py --tournament WNBA        # narrow to one tournament (name substring or id)
  py -3 prophetx_api.py --all                    # every sport, not just basketball (slow: 1 req/s)
  py -3 prophetx_api.py --sandbox                # same against sandbox
  py -3 prophetx_api.py --csv                    # also write prophetx_api_lines.csv
  py -3 prophetx_api.py --raw-event 12345        # dump one event's raw market JSON
  py -3 prophetx_api.py --min-stake 100          # hide selections with < $100 offered

Rate limits honored: 1 request/second per endpoint (ProphetX production base
tier; also their stated query-endpoint cap). Access token refreshed ~every
8 min (expires at 10); auto re-login on 401.
"""
import os, sys, json, time, argparse, csv
import urllib.request, urllib.error
from datetime import datetime, timezone

# Player names carry accents (Azura, Leila); keep the console's own encoding but
# never crash on an unmappable character.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

PROD_BASE = "https://cash.api.prophetx.co/partner"
SANDBOX_BASE = "https://api-ss-sandbox.betprophet.co/partner"

MIN_INTERVAL = 1.05          # seconds between calls to the SAME endpoint
TOKEN_REFRESH_AGE = 8 * 60   # refresh access token after 8 min (expires ~10)
BATCH_EVENT_IDS = 50         # get_multiple_markets caps event_ids at 50

DOCS_DIR = r"C:\Users\User\Documents"

# Basketball-only default: a tournament qualifies if its sport/category field or
# name matches any of these (covers NBA, WNBA, NBA Summer League, NCAAB, Euro).
BASKETBALL_KEYWORDS = ("basketball", "nba", "wnba", "ncaab", "euroleague",
                       "eurocup", "cbb", "g league", "g-league", "big3")


def is_basketball(t):
    """True if a tournament dict looks like basketball. Checks every string
    field (name, category_name, sport_name, nested sport.name, ...)."""
    texts = []
    for v in t.values():
        if isinstance(v, str):
            texts.append(v.lower())
        elif isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str):
                    texts.append(vv.lower())
    blob = " | ".join(texts)
    return any(kw in blob for kw in BASKETBALL_KEYWORDS)


class ProphetXClient:
    """Minimal read-only client. Handles auth, refresh, pacing, retries."""

    def __init__(self, base_url, access_key, secret_key, verbose=True):
        self.base = base_url.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.verbose = verbose
        self.access_token = None
        self.refresh_token = None
        self.token_time = 0.0
        self._last_call = {}     # endpoint path -> monotonic time of last call

    # ── plumbing ──
    def _pace(self, path):
        last = self._last_call.get(path, 0.0)
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[path] = time.monotonic()

    def _request(self, method, path, params=None, body=None, auth=True):
        url = self.base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "prophetx-data-client/1.0")
        if auth:
            req.add_header("Authorization", f"Bearer {self.access_token}")
        self._pace(path)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _call(self, method, path, params=None, body=None, none_on_404=False):
        """Authed call with token refresh, 401 re-login, and 429 backoff.
        none_on_404: ProphetX returns HTTP 404 data_not_found for empty result
        sets (e.g. a tournament with no open events) — return None for those
        instead of raising."""
        if self.access_token and time.monotonic() - self.token_time > TOKEN_REFRESH_AGE:
            self._refresh()
        for attempt in (1, 2):
            try:
                return self._request(method, path, params, body)
            except urllib.error.HTTPError as e:
                if e.code == 404 and none_on_404:
                    return None
                if e.code == 401 and attempt == 1:
                    if self.verbose:
                        print("  [auth] token rejected, re-logging in...")
                    self.login()
                    continue
                if e.code == 429:
                    if self.verbose:
                        print("  [rate] 429 received, backing off 3s...")
                    time.sleep(3)
                    if attempt == 1:
                        continue
                try:
                    detail = e.read().decode()[:300]
                except Exception:
                    detail = ""
                raise RuntimeError(f"{method} {path} -> HTTP {e.code} {detail}") from e

    # ── auth ──
    def login(self):
        resp = self._request("POST", "/auth/login",
                             body={"access_key": self.access_key,
                                   "secret_key": self.secret_key},
                             auth=False)
        data = resp.get("data", {})
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.token_time = time.monotonic()
        if not self.access_token:
            raise RuntimeError(f"login succeeded but no access_token in response: {resp}")
        return data

    def _refresh(self):
        try:
            resp = self._request("POST", "/auth/refresh",
                                 body={"refresh_token": self.refresh_token},
                                 auth=False)
            data = resp.get("data", {})
            tok = data.get("access_token")
            if tok:
                self.access_token = tok
                if data.get("refresh_token"):
                    self.refresh_token = data["refresh_token"]
                self.token_time = time.monotonic()
                return
        except Exception:
            pass
        # refresh failed -> full re-login
        self.login()

    # ── read-only data endpoints ──
    def get_balance(self):
        resp = self._call("GET", "/mm/get_balance")
        return resp.get("data", {})

    def get_tournaments(self):
        resp = self._call("GET", "/mm/get_tournaments")
        return resp.get("data", {}).get("tournaments", []) or []

    def get_sport_events(self, tournament_id):
        resp = self._call("GET", "/mm/get_sport_events",
                          params={"tournament_id": tournament_id}, none_on_404=True)
        if resp is None:
            return []
        return resp.get("data", {}).get("sport_events", []) or []

    def get_multiple_markets(self, event_ids):
        """event_ids: list of ints (<=50). Returns {event_id_str: [markets]}."""
        resp = self._call("GET", "/v2/mm/get_multiple_markets",
                          params={"event_ids": ",".join(str(e) for e in event_ids)},
                          none_on_404=True)
        if resp is None:
            return {}
        return resp.get("data", {}) or {}

    def get_markets(self, event_id):
        resp = self._call("GET", "/v2/mm/get_markets", params={"event_id": event_id},
                          none_on_404=True)
        if resp is None:
            return {}
        return resp.get("data", {}) or {}


# ── formatting helpers ──
def fmt_time(ev):
    """Best-effort start time -> ET string."""
    for key in ("scheduled", "start_time", "scheduled_at", "commence_time", "start_at"):
        val = ev.get(key)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                dt = datetime.fromtimestamp(val, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if ET:
                dt = dt.astimezone(ET)
            return dt.strftime("%a %m/%d %I:%M %p ET")
        except Exception:
            return str(val)
    return ""


def money(x):
    if x is None:
        return "-"
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "-"


def flatten_lines(market):
    """A spread/total market nests alternate lines under market_lines.
    Yield (label, selections) for the market and every sub-line. The label is
    always the PARENT market's name — sub-line names are generic ("Fixed total
    7.5") while the parent carries the identity ("Kamilla Cardoso Total
    Rebounds", "First Half Spread"); the selection itself carries the line."""
    label = market.get("name", "")
    subs = market.get("market_lines") or []
    if subs:
        for sub in subs:
            yield label, sub.get("selections") or []
        if market.get("selections"):
            yield label, market["selections"]
    else:
        yield label, market.get("selections") or []


def selection_levels(sel):
    """A selection is a LIST of liquidity levels ([0] = best) in the mm feed;
    tolerate a bare dict too. Returns (best, total_stake, n_levels)."""
    levels = sel if isinstance(sel, list) else [sel]
    levels = [lv for lv in levels if isinstance(lv, dict)]
    if not levels:
        return None, 0.0, 0
    total = 0.0
    for lv in levels:
        try:
            total += float(lv.get("stake") or 0)
        except (TypeError, ValueError):
            pass
    return levels[0], total, len(levels)


def print_event_markets(ev, markets, min_stake=0.0, csv_rows=None, tournament=""):
    name = ev.get("name") or f"event {ev.get('event_id')}"
    when = fmt_time(ev)
    print(f"\n  {name}" + (f"   [{when}]" if when else "") +
          f"   (event_id {ev.get('event_id')})")
    if not markets:
        print("      no open markets")
        return
    shown = 0
    for mkt in markets:
        mtype = mkt.get("type", "")
        cat = mkt.get("category_name") or ""
        status = mkt.get("status") or ""
        for label, selections in flatten_lines(mkt):
            rows = []
            for sel in selections:
                best, total, n_levels = selection_levels(sel)
                if best is None:
                    continue
                if total < min_stake:
                    continue
                side = best.get("display_name") or best.get("name") or "?"
                odds = best.get("display_odds") or best.get("odds") or "-"
                line = best.get("display_line") or best.get("line")
                rows.append((side, line, odds, best.get("stake"), total, n_levels,
                             best.get("line_id") or best.get("strike_id") or ""))
            if not rows:
                continue
            if shown == 0:
                print(f"      {'Market':<34}{'Side':<26}{'Line':>7}{'Odds':>8}"
                      f"{'Top $':>10}{'Total $':>10}{'Lvls':>5}")
                print("      " + "-" * 100)
            for side, line, odds, top, total, n_levels, line_id in rows:
                line_s = "" if line is None else str(line)
                print(f"      {str(label)[:33]:<34}{str(side)[:25]:<26}{line_s:>7}"
                      f"{str(odds):>8}{money(top):>10}{money(total):>10}{n_levels:>5}")
                if csv_rows is not None:
                    csv_rows.append({
                        "tournament": tournament,
                        "event_id": ev.get("event_id"),
                        "event": name,
                        "start": when,
                        "market_type": mtype,
                        "category": cat,
                        "market": label,
                        "status": status,
                        "side": side,
                        "line": line_s,
                        "odds": odds,
                        "stake_top": top,
                        "stake_total": round(total, 2),
                        "levels": n_levels,
                        "line_id": line_id,
                    })
            shown += 1
    if shown == 0:
        print("      no markets with liquidity above the filter")


def main():
    ap = argparse.ArgumentParser(description="ProphetX official API market data puller (read-only)")
    ap.add_argument("--sandbox", action="store_true",
                    help="use the sandbox environment instead of production")
    ap.add_argument("--tournament", default="",
                    help="tournament filter: name substring or id, comma-separated")
    ap.add_argument("--all", action="store_true",
                    help="pull EVERY sport, not just basketball (slow at 1 req/s)")
    ap.add_argument("--list", action="store_true",
                    help="connectivity check only: login, balance, tournament list")
    ap.add_argument("--min-stake", type=float, default=0.0,
                    help="hide selections with less than this much offered (USD)")
    ap.add_argument("--csv", nargs="?", const=os.path.join(DOCS_DIR, "prophetx_api_lines.csv"),
                    default=None, help="write a CSV (optional path)")
    ap.add_argument("--raw-event", type=int, default=None,
                    help="dump raw market JSON for one event_id and exit")
    ap.add_argument("--balance", action="store_true", help="show balance and exit")
    args = ap.parse_args()

    if args.sandbox:
        base = SANDBOX_BASE
        akey = os.environ.get("PROPHETX_SB_ACCESS_KEY") or os.environ.get("PROPHETX_ACCESS_KEY")
        skey = os.environ.get("PROPHETX_SB_SECRET_KEY") or os.environ.get("PROPHETX_SECRET_KEY")
        env_label = "SANDBOX"
    else:
        base = PROD_BASE
        akey = os.environ.get("PROPHETX_ACCESS_KEY")
        skey = os.environ.get("PROPHETX_SECRET_KEY")
        env_label = "PRODUCTION"

    if not akey or not skey:
        print(f"ERROR: ProphetX API keys not set for {env_label}.")
        print("  Generate them on the ProphetX site: Menu -> API Integration -> Generate New Token")
        print("  Then (PowerShell):")
        if args.sandbox:
            print('    $env:PROPHETX_SB_ACCESS_KEY="<access_key>"')
            print('    $env:PROPHETX_SB_SECRET_KEY="<secret_key>"')
        else:
            print('    $env:PROPHETX_ACCESS_KEY="<access_key>"')
            print('    $env:PROPHETX_SECRET_KEY="<secret_key>"')
        sys.exit(1)

    print("=" * 78)
    print(f"  ProphetX OFFICIAL API — {env_label}  ({base})")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  read-only market data")
    print("=" * 78)

    cli = ProphetXClient(base, akey, skey)
    try:
        cli.login()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        print(f"LOGIN FAILED: HTTP {e.code} {detail}")
        print("  Check the keys, and that they were generated in the SAME environment "
              f"you're targeting ({env_label.lower()}). Sandbox keys don't work in "
              "production and vice versa.")
        sys.exit(1)
    print("  Login OK (access token acquired, auto-refresh enabled).")

    try:
        bal = cli.get_balance()
        if isinstance(bal, dict) and bal:
            b = bal.get("balance", bal)
            print(f"  Balance: {money(b) if not isinstance(b, dict) else json.dumps(b)}")
    except Exception as e:
        print(f"  Balance check skipped ({e})")
    if args.balance:
        return

    if args.raw_event:
        data = cli.get_markets(args.raw_event)
        print(json.dumps(data, indent=2)[:20000])
        return

    tournaments = cli.get_tournaments()
    hoops = [t for t in tournaments if is_basketball(t)]
    print(f"\n  {len(tournaments)} tournaments available "
          f"({len(hoops)} basketball):")
    show = tournaments if (args.all or args.list) else hoops
    for t in show:
        cat = t.get("category_name") or t.get("sport_name") or ""
        tag = "  [BB]" if t in hoops and (args.all or args.list) else ""
        print(f"    {t.get('id'):>6}  {t.get('name','?'):<40} {cat}{tag}")

    if args.list:
        print("\n  (Connectivity check complete. Run without --list to pull basketball "
              "markets and liquidity.)")
        return

    if args.all:
        wanted = tournaments
    elif args.tournament:
        terms = [s.strip().lower() for s in args.tournament.split(",") if s.strip()]
        wanted = []
        for t in tournaments:
            tid, tname = str(t.get("id")), (t.get("name") or "").lower()
            if any(term == tid or term in tname for term in terms):
                wanted.append(t)
        if not wanted:
            print(f"\n  No tournament matched '{args.tournament}'. Use a name or id from the list above.")
            return
    else:
        # DEFAULT: basketball only
        wanted = hoops
        if not wanted:
            print("\n  No basketball tournaments found right now. Use --list to inspect "
                  "the full tournament list (field names may have changed).")
            return

    csv_rows = [] if args.csv else None
    for t in wanted:
        print(f"\n{'=' * 78}\n  {t.get('name')} (tournament {t.get('id')})\n{'=' * 78}")
        events = cli.get_sport_events(t["id"])
        if not events:
            print("  no open events")
            continue
        print(f"  {len(events)} event(s)")
        by_id = {ev.get("event_id"): ev for ev in events if ev.get("event_id") is not None}
        ids = list(by_id.keys())
        for i in range(0, len(ids), BATCH_EVENT_IDS):
            chunk = ids[i:i + BATCH_EVENT_IDS]
            markets_map = cli.get_multiple_markets(chunk)
            for eid in chunk:
                markets = markets_map.get(str(eid)) or markets_map.get(eid) or []
                print_event_markets(by_id[eid], markets, args.min_stake, csv_rows,
                                    tournament=t.get("name", ""))

    if csv_rows is not None:
        if csv_rows:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
                w.writeheader()
                w.writerows(csv_rows)
            print(f"\n  CSV saved: {args.csv}  ({len(csv_rows)} rows)")
        else:
            print("\n  No rows to write to CSV.")


if __name__ == "__main__":
    main()
