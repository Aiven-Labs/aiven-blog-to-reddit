# aiven-blog-to-reddit

Polls https://aiven.io/blog/feed.xml and posts new blogs to r/aiven_ip. Runs on Aiven Runtime with Valkey for dedupe state.

## Deploy
1. Push this folder to a GitHub repo.
2. Create a Valkey service in the same project/cloud.
3. Deploy the app (Runtime > Deploy application), port 8080, integrate the Valkey service as `VALKEY_URL`.
4. In the app's Environment variables, add as **Secret**: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`.

## Testing with older blogs
Set `INITIAL_POST_COUNT=3` (posts the 3 most recent entries on first run, seeds the rest silently), or `DRY_RUN=true` to only log. Afterwards set `INITIAL_POST_COUNT=0`. To re-run the test, delete keys `blog2reddit:seeded` and `blog2reddit:seen` in Valkey.

## Reddit app
Create a "script" app at https://www.reddit.com/prefs/apps with a dedicated bot account that can post in r/aiven_ip.
