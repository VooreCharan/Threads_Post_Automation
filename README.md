# News → Threads agent (all free)

Pulls fresh **Indian** headlines from free RSS feeds (8 categories, 31 feeds from The Hindu, Times of
India, NDTV, Indian Express, Hindustan Times, Mint, Economic Times, Scroll, Gadgets 360 and NDTV
Sports). A chain of free AI providers (Groq, NVIDIA, Gemini, OpenRouter, ...) writes a short original
post that names its source and links the article, and the bot publishes it to your Threads account through the official
Threads API, with the article's own image or a relevant free stock photo when available. You start and
stop it from Telegram.

```
app/sources.py       Indian RSS feeds by category (edit to add/remove)
app/feeds.py         fetch, de-duplicate, filter by age and India keywords
app/article.py       read the full article text (JSON-LD, then main paragraphs; promos removed)
app/llm.py           AI provider chain: writes the post, fact-checks it, judges photos (cached)
app/images.py        article image, or a relevant stock photo (Pexels / Pixabay)
app/bot.py           builds drafts from the newest postable story; publishes approved drafts
app/drafts.py        drafts waiting for your approval (survive restarts, expire)
app/safety.py        sensitive-story filter, new-account warm-up, daily cap and pause
app/telegram_bot.py  Telegram commands, preview with buttons, polling or webhook
app/threads.py       Threads API publish + token renewal
app/maintenance.py   background upkeep: pre-reads stories, renews the token, checks the database
app/alerts.py        warnings to your Telegram (once per cooldown)
app/state.py         posted/skipped stories and settings: SQLite or Turso
app/net.py           shared HTTP connections (reused, safe retries, no cookies)
app/cache.py         small expiring cache for AI results
app/main.py          FastAPI app: /healthz, status, Telegram webhook
app/cli.py           command line: dry runs, --check-llm, --migrate-db
tests/               automated tests (no network, no AI): python -m pytest
```

Categories: india, business, technology, sports, entertainment, health, science, environment.

**India only**: stories mainly about other countries are never posted. Mixed feeds are pre-filtered
by India keywords, and the AI writer then confirms each story is mainly about India (set
`INDIA_ONLY=false` to turn the check off). Rejected stories are remembered and not re-checked.

## One-time setup

1. **Gemini key** (free): https://aistudio.google.com/apikey
2. **Threads API** (free): at https://developers.facebook.com create an app, add the *Threads API*
   use case, and add your Threads account as a Threads Tester (accept the invite in Threads under
   Settings → Account → Website permissions). Generate a user token with `threads_basic` and
   `threads_content_publish`, exchange it for a **long-lived token** (60 days), and note your
   Threads user ID. The app refreshes the token automatically.
3. `cp .env.example .env` and fill in `GEMINI_API_KEY`, `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN`,
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` (see "Using it" below).

## Run

```
pip install -r requirements.txt
python -m app.cli --dry-run --categories health,technology   # preview, posts nothing
python -m app.cli --count 2                                   # really post 2
uvicorn app.main:app                                          # web service
```

## AI providers (free fallback chain)

Posts are written by the first provider in `LLM_CHAIN` that works. Any provider whose key is blank
is skipped, and one that is overloaded or rate-limited is skipped for a few minutes, so a flaky
model doesn't slow every post. Test everything with `python -m app.cli --check-llm`.

| Provider | Get a free key | `.env` key | Notes |
|---|---|---|---|
| Gemini | https://aistudio.google.com/apikey | `GEMINI_API_KEY` | Free tier is small and often overloaded |
| Groq | https://console.groq.com/keys | `GROQ_API_KEY` | Fast; ~30 requests/min, 1,000+/day per model |
| NVIDIA | https://build.nvidia.com (sign in, Settings → API Keys; needs phone verification) | `NVIDIA_API_KEY` | ~40 requests/min across all models; use non-reasoning instruct models |
| OpenRouter | https://openrouter.ai/keys | `OPENROUTER_API_KEY` | `:free` models, ~20/min and ~50/day |
| Hugging Face | https://huggingface.co/settings/tokens (fine-grained, *Inference Providers* permission) | `HF_TOKEN` | Free monthly credits are small; best as a late fallback |
| GitHub Models | https://github.com/settings/tokens (token with *models* access) | `GITHUB_MODELS_TOKEN` | Low daily limits; optional |

Free limits and model names change often. If a model stops working, set a new one in `.env`
(`GROQ_MODELS`, `NVIDIA_MODELS`, `OPENROUTER_MODELS`, `HF_MODELS`, `GITHUB_MODELS`,
`GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`; comma-separated lists are tried in order). To make
another provider the first choice, reorder `LLM_CHAIN`, e.g. `LLM_CHAIN=groq,gemini,openrouter`.

## Images

Default `IMAGE_MODE=auto`, in this order:

1. **The article's own image** (its `og:image`, or the RSS picture): a JPEG/PNG of at least
   `IMAGE_MIN_WIDTH` (600 px), not a logo or placeholder. The post ends with "📷 Image: The Hindu" naming the
   publisher, next to the link to the article.
2. **A relevant licensed stock photo**, when the article has no usable image. The AI writer proposes a
   search phrase such as "solar panels" (stories about named people, politics, crime or specific events
   get none); Pexels / Pixabay candidates are judged by their descriptions and used only if one clearly
   matches. A stock photo carries **no credit of any kind** (no photographer, no site name); the post ends
   with "Source: <publisher>" instead.
3. **No image**: a text post (with Threads' own link-preview card), ending with "Source: <publisher>". This
   also happens whenever Threads rejects an image (the chain is article image -> stock photo -> text), so a
   post is never lost.

You see the exact image in the Telegram draft before anything is posted.

**Stock providers:** Pexels (`PEXELS_API_KEY`, https://www.pexels.com/api/) and Pixabay (`PIXABAY_API_KEY`,
https://pixabay.com/api/docs/) are free and do not require attribution. Unsplash's API terms *do* require
crediting the photographer, so it is not a default; `STOCK_PROVIDERS=pexels,pixabay,unsplash` adds it back,
but then you must show its credit. Providers without a key are skipped.

**Copyright:** a publisher's photo belongs to the publisher. Posting it with credit and a link is common
for news accounts, but it is still their copyright and a complaint can lead to the post, or the account,
being actioned. For the lowest risk set `IMAGE_MODE=stock` (licensed stock photos only); `article` uses
article photos only and `off` posts text only.

## Using it (Telegram)

Only the owner (`TELEGRAM_OWNER_ID`) is answered. Say **hi** and the bot lists the commands:

```
post india | business | technology | sports | entertainment | health | science | environment
status     today's posts, limits and waiting drafts
cancel     discard all waiting drafts
help       show the list again
```

### The post layout
```
The post text, in the writer's own words (at most ~280 characters)

#Sports #ViratKohli            <- the first tag is always the category you asked for; Threads allows one topic tag
https://www.thehindu.com/...   <- the article
📷 Image: The Hindu            <- ONE attribution line: this when the picture is the article's own image,
                                  otherwise "Source: The Hindu" (stock photo or no picture). Never both.
```

### Preview and approve
`post sports` does **not** post. It prepares ONE draft from the latest sports story and shows it in
Telegram exactly as it would appear on Threads, with the photo if any, and three buttons:

- **✅ Post to Threads**: publishes it, then replies with the Threads link and the article link
- **🔄 Another story**: discards this draft (never proposed again) and prepares a different one
- **❌ Skip**: discards it (never proposed again)

Drafts survive restarts and expire after `DRAFT_TTL_MINUTES` (60); up to `MAX_PENDING_DRAFTS` (3) can
wait. Short forms work too (`post tech`, `post sport`, `post news`). Set `REQUIRE_APPROVAL=false` to
publish immediately instead.

### What every draft has been through
- **Full article text**, read from the publisher's page (structured data first, then the page's main
  paragraphs, with ads and "Also Read" lines removed). A story with too little readable text
  (`MIN_ARTICLE_CHARS`) is skipped, because a post cannot be written or checked from a one-line summary.
- **Fact-check**: a second AI pass compares the draft with the article; unsupported claims (who someone
  is, numbers, dates, who said what) cause one rewrite, then the story is skipped.
- **India only**, **no repeats** (the same article, a near-identical headline, or the same news event
  from another outlet), and **no sensitive stories** (violence, sexual crime, suicide, deaths, tragedy;
  `SKIP_SENSITIVE`).
- **Hashtags**: Threads allows only one topic tag per post (the first `#tag` in the text), so the first
  hashtag is always the category you asked for and one or two story-specific names follow; the fact-check
  drops any that are not clearly about the story, and generic filler like `#Trending` is never used.

### Limits that keep the account safe
- At least `MIN_SECONDS_BETWEEN_POSTS` (300 s) between two posts, and at most `DAILY_POST_LIMIT` (10)
  per rolling 24 hours; both are checked again when you tap Post.
- **New-account warm-up**: when the bot first sees a new `THREADS_USER_ID` it allows only 3 posts a day for
  3 days and 5 a day until day 7, then the normal limit (`ACCOUNT_WARMUP`).
- Everything is posted through the official Threads API, always with the article link and the publisher
  named once. Article photos are credited to the publisher; set `IMAGE_MODE=stock` to avoid publishers' photos.
  Consider saying in the account bio that it shares news summaries with sources.

Setup: create a bot with [@BotFather](https://t.me/BotFather) and put its token in
`TELEGRAM_BOT_TOKEN`; start the app, send the bot any message, and it replies with your user id; put
that in `TELEGRAM_OWNER_ID` and restart.

## Testing on your own PC (safely)

Without Telegram and without posting anything:
```powershell
python -m pytest                                   # the automated tests
python -m app.cli --check-llm                      # which AI providers work
python -m app.cli --dry-run --categories sports    # a real draft from live news, printed, not published
```

The full Telegram flow, as a rehearsal (**`DRY_RUN`: tapping Post publishes nothing and the Threads token is
never touched**). Two copies of the bot must never use the same Telegram bot at once, so give your PC its
own test bot (create one with @BotFather; only its token differs) or suspend the Render service first:
```powershell
$env:DRY_RUN = "true"                         # rehearsal: nothing is published
$env:DB_PATH = "test.db"                      # a separate history, your real one is untouched
$env:TELEGRAM_BOT_TOKEN = "<test bot token>"  # NOT the token of the live bot
uvicorn app.main:app --port 8001
```
These `$env:` settings apply only to that PowerShell window. Then in Telegram send `hi`, `status`,
`post technology`, and tap the buttons. Open http://127.0.0.1:8001/ for the app's status.

## Speed, reliability and alerts

- **Fast replies.** Network connections are reused, and every few minutes (`MAINTENANCE_MINUTES`) a
  background job refreshes the feeds and pre-reads the top stories of every category, so a `post ...`
  command typically answers in a few seconds. It makes no AI calls, so it uses none of your free AI quota.
  `PREWARM=false` turns it off.
- **Fewer AI calls.** Writing, fact-check and photo-check results are cached, so the same story is never
  processed twice. The fact-check is deliberately **never skipped**, even for short posts: short posts are
  where an AI is most tempted to fill gaps from memory (a wrong job title for a person with a common name).
- **Alerts on Telegram** (each at most once per cooldown): all AI providers failing (3 requests in a row),
  the database unreachable or not saving, and the Threads token about to expire or failing to renew. The
  token is renewed automatically when under 10 days are left, even if you have not posted. A
  "Bot started" message tells you about every restart (`ALERT_ON_START=false` to silence it).
- **Telegram webhook mode (optional).** Default `TELEGRAM_MODE=polling` keeps asking Telegram for messages,
  so the host must stay awake (hence the UptimeRobot ping on Render's free plan). With
  `TELEGRAM_MODE=webhook` Telegram calls the app instead: a sleeping Render service wakes when you send a
  message, so no keep-alive monitor is needed. Trade-offs: the first reply after a sleep takes about a
  minute, and the background upkeep and alerts only run while the app is awake. Render supplies the public
  URL automatically (`RENDER_EXTERNAL_URL`); elsewhere set `PUBLIC_URL`. Switch back to `polling` any time.

## Development and tests

```
pip install -r requirements-dev.txt
python -m pytest            # ~140 tests, a few seconds, no network, no AI, no keys needed
```

GitHub Actions runs the tests on Python 3.13 and 3.14 for every push, plus a scan that fails the build if
something that looks like an API key is committed. Dependencies are pinned in `requirements.txt` and the
Python version in `.python-version` (what Render runs), so deploys are repeatable.

## Deploy (free, no card): Render + Turso + UptimeRobot

Render's free web service has no persistent disk and sleeps after 15 minutes without traffic, so:
**Turso** (free hosted SQLite) keeps the bot's state (posted history, settings, refreshed Threads
token), and **UptimeRobot** pings `/healthz` every 5 minutes to keep Render awake.

1. **Turso**: sign up at https://turso.tech, create a database (e.g. `threads-bot`), then copy its URL
   (`libsql://threads-bot-<org>.turso.io`) and create a token (database page -> *Create token*).
2. *(Optional, keeps your history and token)* copy your local `bot.db` into Turso:
   ```powershell
   $env:TURSO_URL = "libsql://threads-bot-<org>.turso.io"; $env:TURSO_TOKEN = "<token>"
   python -m app.cli --migrate-db bot.db
   ```
3. **Render**: https://render.com -> sign up with GitHub -> *New* -> *Web Service* -> pick this repo.
   Build command `pip install -r requirements.txt`; start command
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; instance type **Free**. Add every value from
   your local `.env` as an environment variable, plus `TURSO_URL` and `TURSO_TOKEN`. Do not set `DB_PATH`.
4. **UptimeRobot**: https://uptimerobot.com -> *Add monitor* -> HTTP(s) -> URL
   `https://<your-service>.onrender.com/healthz`, interval 5 minutes.
5. *(Optional)* set `TELEGRAM_MODE=webhook` to let the service sleep (see above); then UptimeRobot is not needed.
6. Stop any copy running on your PC (two copies would fight over the Telegram bot), then in Telegram
   send `hi`, then `post sports` and tap **Post to Threads** on the draft.

Without `TURSO_URL`/`TURSO_TOKEN` the bot uses the local `bot.db` file. Any always-on Linux VM with a
disk also works: create a Python venv, `pip install -r requirements.txt`, and run
`uvicorn app.main:app --host 127.0.0.1 --port 8000` under systemd (`Restart=always`).

## Notes

- Threads allows 250 API posts per 24 hours per profile; text posts are limited to 500 characters.
- Posts are written from the article text by an AI, rewritten in its own words, fact-checked against the
  article, and always credit and link the source. Review each publisher's terms of use.
- Serving other people (multi-user) requires Meta app review for `threads_content_publish`.
