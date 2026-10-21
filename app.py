"""Aiven blog -> Reddit helper (semi-automatic, no Reddit API access needed).

Polls the Aiven blog RSS feed and serves a small web page listing recent blogs, each
with a pre-filled "Submit to r/<subreddit>" link. A human clicks it and presses Submit
on Reddit. Blogs already present in the subreddit are marked as posted (detected via
the subreddit's public RSS). Optionally pings Slack when a new blog appears.

Env vars:
  FEED_URL           default https://aiven.io/blog/feed.xml
  SUBREDDIT          default aiven_io
  POLL_SECONDS       default 1800
  LOOKBACK_DAYS      Slack only notifies for blogs published in the last N days (default 3)
  SLACK_WEBHOOK_URL  optional (secret). Slack incoming webhook for new-blog pings.
  SHOW_COUNT         blogs shown on the page (default 25)
  PORT               default 8080
"""
import calendar
import html
import json
import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

import feedparser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("blog2reddit")

FEED_URL = os.getenv("FEED_URL", "https://aiven.io/blog/feed.xml")
SUBREDDIT = os.getenv("SUBREDDIT", "aiven_io")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "1800"))
LOOKBACK_DAYS = float(os.getenv("LOOKBACK_DAYS", "3"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SHOW_COUNT = int(os.getenv("SHOW_COUNT", "25"))
PORT = int(os.getenv("PORT", "8080"))
UA = "aiven-blog-helper/1.0 (feed reader; contact hugh.evans@aiven.io)"

state = {"entries": [], "posted": set(), "reddit_ok": None, "checked": None}
notified = set()  # in memory: a restart can re-ping recent blogs, which is harmless
lock = threading.Lock()


def norm(url):
    return (url or "").split("?")[0].rstrip("/").lower()


def ts(entry):
    t = entry.get("published_parsed")
    return calendar.timegm(t) if t else 0


def submit_url(entry):
    q = urlencode({"type": "LINK", "url": entry.link, "title": entry.title})
    return f"https://www.reddit.com/r/{SUBREDDIT}/submit?{q}"


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace").lower()


def posted_in_subreddit(entries):
    """Return (set_of_normalised_urls_found, ok_flag) using the subreddit's public RSS."""
    try:
        body = fetch_text(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=100")
    except Exception as ex:
        log.warning("could not read r/%s RSS (%s); posted-status unknown", SUBREDDIT, ex)
        return set(), False
    return {norm(e.link) for e in entries if norm(e.link) in body}, True


def slack(entry):
    if not SLACK_WEBHOOK_URL:
        return
    payload = {
        "text": f"New Aiven blog: *{entry.title}*\n<{submit_url(entry)}|Submit to r/{SUBREDDIT}>  ·  <{entry.link}|Read>"
    }
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        log.exception("slack notify failed")


def poll_once():
    feed = feedparser.parse(FEED_URL, agent=UA)
    if not feed.entries:
        log.warning("feed fetch/parse failed: %s", feed.get("bozo_exception"))
        return
    entries = sorted(feed.entries, key=ts, reverse=True)[:SHOW_COUNT]
    posted, ok = posted_in_subreddit(entries)
    with lock:
        state.update(entries=entries, posted=posted, reddit_ok=ok, checked=time.time())
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    for e in reversed(entries):  # oldest first
        if ts(e) >= cutoff and norm(e.link) not in posted and e.link not in notified:
            notified.add(e.link)
            log.info("new blog: %s", e.title)
            slack(e)


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("poll failed")
        time.sleep(POLL_SECONDS)


def render():
    with lock:
        entries, posted, ok, checked = (
            list(state["entries"]), set(state["posted"]), state["reddit_ok"], state["checked"],
        )
    rows = []
    for e in entries:
        done = norm(e.link) in posted
        if done:
            action = "<span class='done'>posted</span>"
        else:
            action = f"<a class='btn' target='_blank' rel='noopener' href='{html.escape(submit_url(e))}'>Submit to r/{html.escape(SUBREDDIT)}</a>"
        date = time.strftime("%Y-%m-%d", time.gmtime(ts(e))) if ts(e) else ""
        rows.append(
            f"<tr><td>{date}</td><td><a href='{html.escape(e.link)}' target='_blank' rel='noopener'>{html.escape(e.title)}</a></td><td>{action}</td></tr>"
        )
    note = ""
    if ok is False:
        note = "<p class='warn'>Couldn't read the subreddit, so posted status may be wrong. Check Reddit before submitting.</p>"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(checked)) if checked else "not yet"
    return f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Aiven blog to r/{html.escape(SUBREDDIT)}</title>
<style>body{{font:15px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}
td{{padding:.5rem;border-bottom:1px solid #ddd}}.btn{{background:#ff4500;color:#fff;padding:.3rem .7rem;border-radius:4px;text-decoration:none;white-space:nowrap}}
.done{{color:#2a7}}.warn{{color:#b60}}</style>
<h1>Aiven blog to r/{html.escape(SUBREDDIT)}</h1>
<p>Click a button, then press Submit on Reddit. Last checked: {when}.</p>{note}
<table>{''.join(rows) or '<tr><td>Loading feed...</td></tr>'}</table>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
