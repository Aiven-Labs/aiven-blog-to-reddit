# aiven-blog-to-reddit

Semi-automatic helper for sharing new Aiven blogs on r/aiven_io. Needs no Reddit API access.

Runs as one container on Aiven Runtime. It polls https://aiven.io/blog/feed.xml and serves a page at the app's public URL listing recent blogs, each with a pre-filled "Submit to r/aiven_io" link. Click it, press Submit on Reddit. Blogs already in the subreddit show as "posted" (detected from the subreddit's public RSS).

Optional: set `SLACK_WEBHOOK_URL` (as a Secret) to get a Slack ping with the submit link for each new blog.

## Deploy
Aiven Console > Runtime > Deploy application from this repo, port 8080. Env vars: `SUBREDDIT` (default `aiven_io`), `POLL_SECONDS`, `LOOKBACK_DAYS`, `SLACK_WEBHOOK_URL`, `SHOW_COUNT`.

## Notes
- The page is public and only lists public blog titles.
- Slack dedupe is in memory, so a restart may re-ping blogs from the last `LOOKBACK_DAYS` days.
- If Reddit blocks the RSS check from the cloud IP, the page warns that posted status is unknown.
