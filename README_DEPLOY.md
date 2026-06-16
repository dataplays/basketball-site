# Your Basketball Site — How to Put It Online

This folder is a complete website that runs **all** your basketball programs in one
place, 24/7, reachable from any phone or computer. You don't need your own physical
server — a hosting company runs it for you. This guide gets it live in about 30 minutes.

---

## What's in this folder

**The site:**
- `wsgi.py` — the "front door." Shows a homepage that links to every dashboard, plus a
  Tools page. You don't edit this.

**Your 6 live dashboards** (each becomes a page on the site):
- `nba_live_projections.py` → **/nba**
- `wnba_live_projections.py` → **/wnba**
- `cbb_live_projections.py` → **/cbb** (men's college)
- `wcbb_live_projections.py` → **/wcbb** (women's college)
- `nbl_live_projections.py` → **/nbl** (Australian NBL)
- `intl_live_projections.py` → **/intl** (international)

**Your tools** (run on demand from the site's Tools page):
- `nba_props_projections.py`, `wnba_props_projections.py`, `wnba_props_track.py`
- `wnba_props_grade.py` — a helper the tools use (not a button)
- `cbb_pace_daily.py`, `cbb_power_pace_daily.py` — generate the daily CBB report PDFs
  (run automatically each day; see below)

**Support files:** `requirements.txt`, `Procfile`, `runtime.txt`, and two folders:
`data/` (where ratings CSVs live) and `reports/` (where generated PDFs land).

---

## Before you upload: the data files

Your programs were changed in one small way — they no longer look in
`C:\Users\User\Documents`. Instead they look in the **`data/`** folder next to the code.

Here's what each dashboard needs:

| Dashboard | Needs a data file? |
|-----------|--------------------|
| NBA, WNBA | No — they build their own ratings automatically on first load. |
| NBL, International | No — they pull everything live. |
| CBB (men's college) | Yes — `data/cbb_pace_ratings_2026.csv` (included) |
| WCBB (women's college) | Yes — `data/wcbb_pace_ratings_2026.csv` (included) |

If a CBB/WCBB file is missing, that dashboard still runs — it just uses average pace
until you add the real file. So you can launch first and add data later.

**To add your data:** copy your `*_ratings_*.csv` files into the `data/` folder before
uploading (or upload them to the site's `data/` folder afterward).

---

## Recommended host: Render (~$7/month, always on)

Render runs Python apps continuously, gives you a public web address, has full internet
access (your dashboards need that to fetch live scores), and lets your Tools page run on
demand. The free plan works but "sleeps" after 15 minutes idle (slow first load); the
$7/month plan stays on 24/7, which is what you asked for.

### Step 1 — Put the folder on GitHub (no command line needed)
1. Make a free account at **github.com**.
2. Click the **+** (top right) → **New repository**. Name it `basketball-site`,
   keep it **Private**, click **Create repository**.
3. On the new repo page, click **uploading an existing file**.
4. Drag in **everything from this folder** (all the `.py` files, `requirements.txt`,
   `Procfile`, `runtime.txt`, and the `data/` folder with your CSVs). Click
   **Commit changes**.

### Step 2 — Deploy on Render
1. Make a free account at **render.com** and choose **"Sign in with GitHub."**
2. Click **New +** → **Web Service** → connect your `basketball-site` repo.
3. Render auto-detects Python. Confirm these settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn wsgi:application`
4. Pick the **Starter ($7/mo)** instance for always-on (or **Free** to try it).
5. Click **Create Web Service**. Wait ~3–5 minutes for the first build.

When it finishes, Render gives you a URL like
`https://basketball-site.onrender.com`. Open it — you'll see your homepage with all six
dashboards and the Tools page. That address works from anywhere, on any device.

### Step 3 — Updating later
Whenever you change a program: on GitHub, open the file (or use **Add file → Upload
files**), replace it, and **Commit**. Render automatically rebuilds and redeploys within
a few minutes. No need to touch Render again.

### Optional — a custom name
In Render → your service → **Settings → Custom Domain**, you can attach something like
`mybasketball.com` (buy the name at any registrar, ~$12/year).

---

## Alternative host: Railway (~$5/month, usage-based)
Same idea, slightly cheaper if traffic is light. Sign in at **railway.app** with GitHub,
**New Project → Deploy from GitHub repo**, pick `basketball-site`. Railway reads the
`Procfile` automatically. You get a public URL under **Settings → Generate Domain**.

---

## The daily CBB reports refresh automatically (already set up)
You asked for this and it's built in. The site runs a background scheduler that
regenerates the daily CBB report(s) every day at **6:00 AM Eastern** and links the
latest PDFs on the homepage under "Tools & Reports." It also generates them once on
first launch if none exist yet. Nothing for you to configure.

- **Change the time:** on your host, add an environment variable
  `CBB_REFRESH_HOUR` set to an hour 0-23 (Eastern). Example: `7` = 7 AM. On Render
  that's **Settings -> Environment -> Add Environment Variable**.
- **About the pace ratings:** the `*_pace_ratings_2026.csv` files are inputs **you**
  maintain (both are already included in the `data/` folder). The daily scripts read
  them and pull fresh schedules and efficiency numbers on each run; they don't
  recompute the pace numbers themselves. To update pace, replace the CSV in `data/`.

## Test it on your own PC first (optional)
If you want to preview before paying for hosting, in a command prompt in this folder:

```
pip install -r requirements.txt
python wsgi.py
```

Then open **http://localhost:8000** in your browser.

---

## Quick troubleshooting
- **A dashboard shows odd/average numbers:** its ratings CSV isn't in `data/` yet. Add it.
- **A Tools button is greyed out:** a file it needs is missing from the folder.
- **"Application failed to respond" on Render free plan:** it went to sleep; wait ~30s
  for it to wake, or upgrade to Starter for always-on.
