#!/usr/bin/env python3
"""
contact_page.py — Contact / Feedback page (/contact).
=====================================================
A "Contact Us" page for the basketball-site: a feedback form plus a direct
email link to the site owner (dataplays@yahoo.com).

Where feedback goes
-------------------
1. Always appended to a JSONL file (`feedback.jsonl` in BBALL_DATA_DIR if
   set, else the Documents folder locally). NOTE: Render's disk is
   ephemeral — the file survives until the next deploy/cold start — so:
2. If the FEEDBACK_SMTP_* env vars are set, each submission is ALSO emailed
   to FEEDBACK_TO (default dataplays@yahoo.com) — the reliable delivery
   path in production. Dormant until configured (same pattern as the
   alerts watcher's EmailNotifier). For Yahoo Mail:
       FEEDBACK_SMTP_HOST=smtp.mail.yahoo.com
       FEEDBACK_SMTP_PORT=465
       FEEDBACK_SMTP_USER=dataplays@yahoo.com
       FEEDBACK_SMTP_PASS=<Yahoo app password — account settings>
       FEEDBACK_TO=dataplays@yahoo.com
3. Optional inbox view at /inbox?key=<FEEDBACK_INBOX_KEY> (404 unless the
   env var is set) to read the stored JSONL in a browser.

Anti-spam: honeypot field + per-IP 30s cooldown + length caps. The email
link is assembled in JS so the address isn't scrapeable as plain text.

Run standalone:
    py -3 contact_page.py            # http://localhost:5023
Mounted at /contact on the basketball-site (module-level `app`, relative
form action so it works under the mount).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, request

ACCENT = "#64b5f6"
OWNER_USER = "dataplays"          # email assembled client-side: user + @ + domain
OWNER_DOMAIN = "yahoo.com"
MAX_MSG = 4000
MAX_FIELD = 120
COOLDOWN_S = 30

_last_post: dict = {}             # ip -> monotonic ts (per-process cooldown)
_lock = threading.Lock()


def _feedback_path() -> Path:
    data_dir = os.environ.get("BBALL_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "feedback.jsonl"
    docs = Path(r"C:\Users\User\Documents")
    if docs.is_dir():
        return docs / "basketball_site_feedback.jsonl"
    return Path(__file__).resolve().parent / "feedback.jsonl"


def _store(entry: dict) -> bool:
    try:
        path = _feedback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _send_email(entry: dict) -> bool:
    """Email the feedback to the owner if SMTP env vars are configured."""
    host = os.environ.get("FEEDBACK_SMTP_HOST")
    user = os.environ.get("FEEDBACK_SMTP_USER")
    pw = os.environ.get("FEEDBACK_SMTP_PASS")
    if not (host and user and pw):
        return False
    to = os.environ.get("FEEDBACK_TO", f"{OWNER_USER}@{OWNER_DOMAIN}")
    port = int(os.environ.get("FEEDBACK_SMTP_PORT", "465"))
    try:
        msg = EmailMessage()
        msg["Subject"] = "Basketball-site feedback" + (
            f" from {entry['name']}" if entry.get("name") else "")
        msg["From"] = user
        msg["To"] = to
        if entry.get("email"):
            msg["Reply-To"] = entry["email"]
        msg.set_content(
            f"Time: {entry['ts']}\nName: {entry.get('name') or '-'}\n"
            f"Email: {entry.get('email') or '-'}\nPage: {entry.get('ref') or '-'}\n"
            f"\n{entry['message']}\n")
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:
        return False


def handle_post() -> tuple[str, str]:
    """Returns (status, note) for the template."""
    if (request.form.get("website") or "").strip():
        return "ok", "Thanks — your feedback has been received."   # honeypot
    msg = (request.form.get("message") or "").strip()
    if not msg:
        return "err", "Please write a message before sending."
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.monotonic()
    with _lock:
        if now - _last_post.get(ip, -1e9) < COOLDOWN_S:
            return "err", "Easy there — please wait a few seconds between messages."
        _last_post[ip] = now
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "name": (request.form.get("name") or "").strip()[:MAX_FIELD],
        "email": (request.form.get("email") or "").strip()[:MAX_FIELD],
        "message": msg[:MAX_MSG],
        "ref": (request.form.get("ref") or "").strip()[:MAX_FIELD],
        "ip": ip,
    }
    stored = _store(entry)
    mailed = _send_email(entry)
    if mailed:
        return "ok", "Thanks — your feedback is on its way."
    if stored:
        return "ok", "Thanks — your feedback has been received."
    return "err", "Sorry, something went wrong saving your message — please use the email link below instead."


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f1419">
<title>Contact Us</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--mut:#8a95a5;
--accent:{{ACCENT}};--up:#4caf50;--dn:#e57373;}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);margin:0;padding:22px}
.wrap{max-width:640px;margin:0 auto}
h1{font-size:23px;margin:0 0 3px}h1 span{color:var(--accent)}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:18px 20px;margin-bottom:16px}
label{display:block;color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin:14px 0 5px}
label:first-child{margin-top:0}
input[type=text],input[type=email],textarea{width:100%;padding:10px 12px;
background:#0f1419;color:var(--text);border:1px solid var(--border);border-radius:6px;
font-size:14px;font-family:inherit}
input:focus,textarea:focus{outline:none;border-color:var(--accent)}
textarea{min-height:140px;resize:vertical}
.btn{padding:10px 22px;background:var(--accent);color:#0f1419;border:0;border-radius:6px;
font-size:14px;font-weight:700;cursor:pointer;margin-top:16px}
.btn:hover{filter:brightness(1.12)}
.note{color:var(--mut);font-size:12.5px;margin-top:10px;line-height:1.5}
.flash{border-radius:8px;padding:12px 14px;margin-bottom:16px;font-size:14px}
.flash.ok{background:rgba(76,175,80,.12);border:1px solid rgba(76,175,80,.45);color:#a5d6a7}
.flash.err{background:rgba(229,115,115,.12);border:1px solid rgba(229,115,115,.45);color:#ef9a9a}
.mailrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
a.mail{color:var(--accent);font-weight:700;font-size:15px;text-decoration:none;
border:1px solid var(--accent);border-radius:8px;padding:10px 18px;display:inline-block}
a.mail:hover{background:rgba(100,181,246,.12)}
.hp{position:absolute;left:-9999px;top:-9999px;height:1px;width:1px;overflow:hidden}
</style></head><body><div class=wrap>
<h1>Contact <span>Us</span></h1>
<div class=sub>Feedback, bug reports, feature ideas — or just say what you'd like to see next.</div>
{{FLASH}}
<form method=post>
<div class=panel>
  <label>Name (optional)</label>
  <input type=text name=name maxlength=120 value="{{NAME}}">
  <label>Your email (optional — only if you'd like a reply)</label>
  <input type=email name=email maxlength=120 value="{{EMAIL}}">
  <label>Message</label>
  <textarea name=message maxlength=4000 required placeholder="What's on your mind?">{{MESSAGE}}</textarea>
  <div class=hp><label>Website</label><input type=text name=website tabindex=-1 autocomplete=off></div>
  <input type=hidden name=ref value="{{REF}}">
  <button type=submit class=btn>Send Feedback</button>
  <div class=note>Messages go straight to the site owner. No account, no tracking.</div>
</div>
</form>
<div class=panel>
  <div class=mailrow>
    <a class=mail id=maillink href="#">Email me directly</a>
    <span class=note id=mailaddr style="margin-top:0"></span>
  </div>
  <div class=note>Prefer email? The button opens your mail app.</div>
</div>
<div class=note style="text-align:center;margin-top:20px"><a href="../" style="color:var(--mut)">&larr; Main Menu</a></div>
</div>
<script>
(function(){
  var u="{{OWNER_USER}}", d="{{OWNER_DOMAIN}}", a=u+"\\u0040"+d;
  var l=document.getElementById("maillink");
  l.href="mailto:"+a+"?subject="+encodeURIComponent("Basketball site feedback");
  document.getElementById("mailaddr").textContent=a;
})();
</script>
</body></html>"""

INBOX = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Feedback Inbox</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1419;
color:#e8ecf1;margin:0;padding:22px}.wrap{max-width:760px;margin:0 auto}
h1{font-size:20px}.m{background:#1a2029;border:1px solid #2a3340;border-radius:10px;
padding:14px 16px;margin-bottom:12px;white-space:pre-wrap;word-break:break-word}
.h{color:#8a95a5;font-size:12px;margin-bottom:8px}</style></head><body><div class=wrap>
<h1>Feedback Inbox ({{N}})</h1>{{ITEMS}}</div></body></html>"""

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#0f1419"/>'
           '<path d="M5 9 h22 v14 h-22 z" fill="none" stroke="#64b5f6" stroke-width="2.2"/>'
           '<path d="M5 10 l11 8 11-8" fill="none" stroke="#64b5f6" stroke-width="2.2"/></svg>')


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def render_page(status: str | None, note: str | None, keep: dict | None = None) -> str:
    keep = keep or {}
    flash = (f'<div class="flash {status}">{_esc(note)}</div>' if status else "")
    return (PAGE.replace("{{ACCENT}}", ACCENT)
                .replace("{{FLASH}}", flash)
                .replace("{{NAME}}", _esc(keep.get("name", "")))
                .replace("{{EMAIL}}", _esc(keep.get("email", "")))
                .replace("{{MESSAGE}}", _esc(keep.get("message", "")))
                .replace("{{REF}}", _esc(keep.get("ref", "")))
                .replace("{{OWNER_USER}}", OWNER_USER)
                .replace("{{OWNER_DOMAIN}}", OWNER_DOMAIN))


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            status, note = handle_post()
            keep = ({} if status == "ok" else
                    {k: request.form.get(k, "") for k in ("name", "email", "message", "ref")})
            return render_page(status, note, keep)
        return render_page(None, None, {"ref": request.headers.get("Referer", "")[:120]})

    @app.route("/inbox")
    def inbox():
        key = os.environ.get("FEEDBACK_INBOX_KEY")
        if not key or request.args.get("key") != key:
            return ("Not found.", 404, {"Content-Type": "text/plain"})
        items = []
        try:
            with open(_feedback_path(), "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    items.append(
                        f'<div class=m><div class=h>{_esc(e.get("ts"))} · '
                        f'{_esc(e.get("name") or "anon")} · {_esc(e.get("email") or "-")} · '
                        f'{_esc(e.get("ip") or "")}</div>{_esc(e.get("message"))}</div>')
        except FileNotFoundError:
            pass
        items.reverse()
        return (INBOX.replace("{{N}}", str(len(items)))
                     .replace("{{ITEMS}}", "".join(items) or "<div class=m>(empty)</div>"))

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON, 200, {"Content-Type": "image/svg+xml"})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser(description="Contact / feedback page")
    ap.add_argument("--port", type=int, default=5023)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"Contact page -> http://{args.host}:{args.port}")
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
