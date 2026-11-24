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
import urllib.error
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
# Reddit asks for a descriptive UA naming the app and a Reddit username (no email needed).
UA = os.getenv("USER_AGENT", "python:aiven-blog-helper:1.0 (by /u/HughEvansDev)")
STATE_FILE = os.getenv("STATE_FILE", "/tmp/posted.json")

state = {"entries": [], "posted": {}, "reddit_ok": None, "checked": None, "reddit_last_ok": None}
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


def fetch_text(url, attempts=3):
    """GET with retries: Reddit throttles unauthenticated requests (429) from time to time."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (429, 500, 502, 503, 504):
                break
        except Exception as ex:  # network hiccup
            last = ex
        time.sleep(2 * (i + 1))
    raise last


def reddit_endpoints():
    s = SUBREDDIT
    return [
        f"https://www.reddit.com/r/{s}/new/.rss?limit=100",
        f"https://www.reddit.com/r/{s}/search.rss?q=aiven.io&restrict_sr=1&sort=new&limit=100",
        f"https://old.reddit.com/r/{s}/new/.rss?limit=100",
    ]


def load_saved():
    try:
        with open(STATE_FILE) as f:
            return {k: tuple(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def save_posted(posted):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(posted, f)
    except Exception:
        pass


def posted_in_subreddit(entries):
    """Return ({normalised blog url: (unix_ts, reddit_permalink)}, ok_flag).

    Uses the subreddit's public RSS (trying a few endpoints). Each Reddit entry carries
    its posted time and its permalink; the blog URL it points to is inside the content.
    """
    feed = None
    for url in reddit_endpoints():
        try:
            parsed = feedparser.parse(fetch_text(url))
        except Exception as ex:
            log.warning("could not read %s (%s)", url, ex)
            continue
        if parsed.get("feed", {}).get("title", "").lower() == "not found":
            log.warning("%s returned a Not Found feed", url)
            continue
        feed = parsed
        break
    if feed is None:
        log.warning("all Reddit endpoints failed for r/%s; keeping last known status", SUBREDDIT)
        return {}, False
    wanted = {norm(e.link): e.link for e in entries}
    found = {}
    for r in feed.entries:
        content = html.unescape(
            " ".join(c.get("value", "") for c in r.get("content", [])) + " " + r.get("summary", "")
        ).lower()
        when = calendar.timegm(r.published_parsed) if r.get("published_parsed") else 0
        for n in wanted:
            if n in content and (n not in found or when < found[n][0]):
                found[n] = (when, r.get("link", ""))
    return found, True


def merge_posted(entries):
    """Check Reddit and merge into state. A blog once seen as posted stays posted, so a
    temporary Reddit outage never wipes known status."""
    found, ok = posted_in_subreddit(entries)
    with lock:
        merged = dict(state["posted"])
        merged.update(found)
        state.update(posted=merged, reddit_ok=ok, checked=time.time())
        if ok:
            state["reddit_last_ok"] = time.time()
    if ok and found:
        save_posted(merged)
    return merged


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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
    with lock:
        state.update(entries=entries)
        if not state["posted"]:
            state["posted"] = load_saved()
    posted = merge_posted(entries)
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    with lock:
        global _last_refresh
        _last_refresh = time.time()
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


REFRESH_TTL = 60  # seconds: page loads re-check the subreddit at most this often
_last_refresh = 0.0


def refresh_posted():
    """Re-check which blogs are already in the subreddit (cheap; called on page load)."""
    global _last_refresh
    with lock:
        entries = list(state["entries"])
        if not entries or time.time() - _last_refresh < REFRESH_TTL:
            return
        _last_refresh = time.time()
    merge_posted(entries)


def render():
    refresh_posted()
    with lock:
        entries, posted, ok, checked = (
            list(state["entries"]), dict(state["posted"]), state["reddit_ok"], state["checked"],
        )
    now = time.time()
    rows = []
    for e in entries:
        info = posted.get(norm(e.link))
        if info:
            when, permalink = info
            stamp = time.strftime("%a %d %b %H:%M UTC", time.gmtime(when)) if when else "time unknown"
            rel = f" ({ago(now - when)})" if when else ""
            label = f"posted {html.escape(stamp)}{rel}"
            if permalink:
                label = f"<a href='{html.escape(permalink)}' target='_blank' rel='noopener'>{label}</a>"
            action = f"<span class='done'>{label}</span>"
        else:
            action = f"<a class='btn' target='_blank' rel='noopener' href='{html.escape(submit_url(e))}'>Submit to r/{html.escape(SUBREDDIT)}</a>"
        date = time.strftime("%Y-%m-%d", time.gmtime(ts(e))) if ts(e) else ""
        rows.append(
            f"<tr><td>{date}</td><td><a href='{html.escape(e.link)}' target='_blank' rel='noopener'>{html.escape(e.title)}</a></td><td>{action}</td></tr>"
        )
    note = ""
    if ok is False:
        last_ok = state.get("reddit_last_ok")
        since = f"Showing last known status (Reddit last read {ago(now - last_ok)})." if last_ok else "No status known yet."
        note = (
            "<p class='warn'>Reddit is not letting this app read r/" + html.escape(SUBREDDIT)
            + " right now (it throttles automated requests), so posted status may be out of date. "
            + since + " Check the subreddit before submitting.</p>"
        )
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(checked)) if checked else "not yet"
    times = [w for w, _ in posted.values() if w]
    if times:
        n24 = sum(1 for w in times if now - w < 86400)
        n7 = sum(1 for w in times if now - w < 7 * 86400)
        pace = (
            f"<p class='pace'>Pacing: last blog post {ago(now - max(times))} &middot; "
            f"{n24} in the last 24h &middot; {n7} in the last 7 days.</p>"
        )
    elif ok is False:
        pace = "<p class='pace'>Pacing: unavailable until Reddit can be read.</p>"
    else:
        pace = "<p class='pace'>Pacing: no blog posts found in the subreddit yet.</p>"
    return f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Aiven blog to r/{html.escape(SUBREDDIT)}</title>
<style>body{{font:15px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}
td{{padding:.5rem;border-bottom:1px solid #ddd}}.btn{{background:#ff4500;color:#fff;padding:.3rem .7rem;border-radius:4px;text-decoration:none;white-space:nowrap}}
.done{{color:#2a7}}.done a{{color:inherit}}.warn{{color:#b60}}.pace{{font-weight:600}}</style>
<h1>Aiven blog to r/{html.escape(SUBREDDIT)}</h1>
<p>Click a button, then press Submit on Reddit. Last checked: {when}.</p>{pace}{note}
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
