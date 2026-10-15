# aiven-blog-to-reddit

Polls https://aiven.io/blog/feed.xml and posts new blogs to r/aiven_ip. Runs as a single container on Aiven Runtime. No database: the subreddit itself is the dedupe state (it skips any blog URL already posted there).

## Deploy
1. Push this repo to GitHub.
2. Aiven Console > Runtime > Deploy application, port 8080.
3. In the app's Environment variables, add as **Secret**: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`.

## Testing with older blogs
Set `LOOKBACK_DAYS=60` and `MAX_PER_RUN=3` (posts the 3 oldest unposted blogs in the window per poll). Add `DRY_RUN=true` to only log. For production use `LOOKBACK_DAYS=3`.

## Reddit app
Create a "script" app at https://www.reddit.com/prefs/apps with a dedicated bot account that can post in r/aiven_ip.
