"""Poll the Aiven blog RSS feed and post new entries to a subreddit.

State (already-posted URLs) lives in Valkey so restarts/redeploys don't repost.

Env vars:
  VALKEY_URL            injected by the Aiven Valkey service integration
  REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USERNAME / REDDIT_PASSWORD  (secrets)
  FEED_URL              default https://aiven.io/blog/feed.xml
  SUBREDDIT             default aiven_ip
  POLL_SECONDS          default 1800
  INITIAL_POST_COUNT    on the very first run, post the N most recent entries and mark
                        the rest as seen (default 0 = seed silently). Use e.g. 3 to test.
  DRY_RUN               "true" = log what would be posted, post nothing, save no state
  PORT                  health endpoint port, default 8080
"""
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import feedparser
import praw
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("blog2reddit")

FEED_URL = os.getenv("FEED_URL", "https://aiven.io/blog/feed.xml")
SUBREDDIT = os.getenv("SUBREDDIT", "aiven_ip")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "1800"))
INITIAL_POST_COUNT = int(os.getenv("INITIAL_POST_COUNT", "0"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
PORT = int(os.getenv("PORT", "8080"))

SEEN_KEY = "blog2reddit:seen"
SEEDED_KEY = "blog2reddit:seeded"


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


def submit(reddit, entry):
    if DRY_RUN:
        log.info("[dry-run] would post: %s -> %s", entry.title, entry.link)
        return True
    try:
        reddit.subreddit(SUBREDDIT).submit(title=entry.title, url=entry.link)
        log.info("posted: %s", entry.title)
        return True
    except Exception:
        log.exception("failed to post %s", entry.link)
        return False


def poll_once(r, reddit):
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        log.warning("feed fetch/parse failed: %s", feed.get("bozo_exception"))
        return
    entries = list(feed.entries)  # newest first

    if not r.exists(SEEDED_KEY):
        to_post = entries[:INITIAL_POST_COUNT]
        to_seed = entries[INITIAL_POST_COUNT:]
        log.info("first run: posting %d, silently seeding %d", len(to_post), len(to_seed))
        if not DRY_RUN:
            if to_seed:
                r.sadd(SEEN_KEY, *[e.link for e in to_seed])
            r.set(SEEDED_KEY, "1")
        for e in reversed(to_post):  # oldest first
            if submit(reddit, e) and not DRY_RUN:
                r.sadd(SEEN_KEY, e.link)
        return

    new = [e for e in entries if not r.sismember(SEEN_KEY, e.link)]
    log.info("%d new entries", len(new))
    for e in reversed(new):  # oldest first
        if submit(reddit, e) and not DRY_RUN:
            r.sadd(SEEN_KEY, e.link)
        time.sleep(10)  # be gentle with Reddit rate limits


def main():
    threading.Thread(target=serve_health, daemon=True).start()
    r = redis.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    reddit = None if DRY_RUN else reddit_client()
    while True:
        try:
            poll_once(r, reddit)
        except Exception:
            log.exception("poll failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
