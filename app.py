"""Poll the Aiven blog RSS feed and post new entries to a subreddit.

No database: Reddit itself is the state. Before posting, the app reads the
subreddit's recent submissions and skips any blog URL already there. Reddit's own
duplicate-link check is a second safety net.

Env vars (Reddit ones are secrets):
  REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USERNAME / REDDIT_PASSWORD
  FEED_URL        default https://aiven.io/blog/feed.xml
  SUBREDDIT       default aiven_ip
  POLL_SECONDS    default 1800
  LOOKBACK_DAYS   only consider blogs published in the last N days (default 3).
                  Raise it (e.g. 60) to test with older blogs.
  MAX_PER_RUN     max posts per poll (default 3)
  DRY_RUN         "true" = log what would be posted, post nothing
  PORT            health endpoint port, default 8080
"""
import calendar
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import feedparser
import praw
from praw.exceptions import RedditAPIException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("blog2reddit")

FEED_URL = os.getenv("FEED_URL", "https://aiven.io/blog/feed.xml")
SUBREDDIT = os.getenv("SUBREDDIT", "aiven_ip")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "1800"))
LOOKBACK_DAYS = float(os.getenv("LOOKBACK_DAYS", "3"))
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "3"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
PORT = int(os.getenv("PORT", "8080"))


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def serve_health():
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()


def reddit_client():
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=f"aiven-blog-poster by u/{os.environ['REDDIT_USERNAME']}",
    )


def norm(url):
    return (url or "").split("?")[0].rstrip("/").lower()


def already_posted(sub):
    return {norm(s.url) for s in sub.new(limit=200)}


def published_ts(entry):
    t = entry.get("published_parsed")
    return calendar.timegm(t) if t else 0


def poll_once(reddit):
    feed = feedparser.parse(FEED_URL)
    if not feed.entries:
        log.warning("feed fetch/parse failed: %s", feed.get("bozo_exception"))
        return
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    sub = reddit.subreddit(SUBREDDIT)
    posted = already_posted(sub)

    candidates = [
        e for e in feed.entries
        if published_ts(e) >= cutoff and norm(e.link) not in posted
    ]
    candidates.sort(key=published_ts)  # oldest first
    log.info("%d candidate(s); posting up to %d", len(candidates), MAX_PER_RUN)

    for e in candidates[:MAX_PER_RUN]:
        if DRY_RUN:
            log.info("[dry-run] would post: %s -> %s", e.title, e.link)
            continue
        try:
            sub.submit(title=e.title, url=e.link, resubmit=False)
            log.info("posted: %s", e.title)
        except RedditAPIException as ex:
            log.warning("reddit rejected %s: %s", e.link, ex)
        except Exception:
            log.exception("failed to post %s", e.link)
        time.sleep(10)


def main():
    threading.Thread(target=serve_health, daemon=True).start()
    reddit = reddit_client()
    while True:
        try:
            poll_once(reddit)
        except Exception:
            log.exception("poll failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
