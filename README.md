# News → Threads agent (all free)

Pulls fresh **Indian** headlines from free RSS feeds (8 categories, 31 feeds from The Hindu, Times of
India, NDTV, Indian Express, Hindustan Times, Mint, Economic Times, Scroll, Gadgets 360 and NDTV
Sports). A chain of free AI providers (Groq, NVIDIA, Gemini, OpenRouter, ...) writes a short original
post with a source credit, and the bot publishes it to your Threads account through the official
Threads API, with the article's own image or a relevant free stock photo when available. You start and
stop it from Telegram.

```
app/sources.py       Indian RSS feeds by category (edit to add/remove)
app/feeds.py         fetch, de-duplicate, filter by age and India keywords
app/llm.py           AI provider chain: writes the post, checks it is about India, judges photos
app/images.py        article image, or a relevant stock photo (Pexels / Pixabay / Unsplash)
app/threads.py       Threads API publish + automatic token refresh
app/bot.py           the posting loop: picks stories, publishes with random gaps, daily limit
app/telegram_bot.py  "start posting" / "stop posting" / "post now" / "status" from Telegram
app/state.py         SQLite: posted/skipped stories, settings, refreshed token
app/main.py          FastAPI app: /healthz, status, POST /api/trigger, scheduler heartbeat
app/cli.py           command line: dry runs and --check-llm
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

Every post tries to carry a picture, in this order (`IMAGE_MODE=auto`):

1. **The article's own image** (its `og:image`, or the RSS picture): a JPEG/PNG of at least
   `IMAGE_MIN_WIDTH` (600 px), not a logo or placeholder.
2. **A relevant free stock photo**, only when the story has a concrete photographable subject (the AI
   writer proposes a search phrase such as "solar panels"; stories about people, politics, crime or
   specific events get none). Candidates from Pexels / Pixabay / Unsplash are then judged by their
   descriptions and used only if one clearly matches; otherwise nothing is used. The post credits the
   photographer.
3. **No image**: a normal text post with a link preview. This also happens whenever Threads rejects an
   image, so a post is never lost.

Free stock-photo keys (all free, no card): Pexels https://www.pexels.com/api/ (`PEXELS_API_KEY`),
Pixabay https://pixabay.com/api/docs/ (`PIXABAY_API_KEY`), Unsplash https://unsplash.com/developers
(`UNSPLASH_ACCESS_KEY`, demo apps are limited to 50 requests/hour). Providers without a key are skipped.

Other modes: `IMAGE_MODE=article` (article photos only), `stock` (stock only), `off` (text only).
Reusing a publisher's photo is subject to their copyright and terms; the link back to the source is
always included.

## Using it (Telegram)

Only the owner (`TELEGRAM_OWNER_ID`) is answered. Say **hi** and the bot lists the commands:

```
post india | business | technology | sports | entertainment | health | science | environment
status     today's post count and the latest posts
help       show the list again
```

**`post sports`** publishes ONE latest update from that category to Threads, then replies with the
Threads link and the article link. Short forms work too (`post tech`, `post sport`, `post news`).
If you already know the commands you can send them directly.

Every post ends with up to three hashtags. Threads allows only **one topic tag per post** (the first
`#tag` in the text, shown beside your name), so the first hashtag is always the category you asked for
(`#Sports`, `#Entertainment`, ...) and the next one or two are story-specific names taken from the
article (`#ViratKohli`, `#RBI`). The AI proposes them and the fact-check pass drops any that are not
clearly about the story; generic filler like `#Trending` is never used.

Each post is also fact-checked against its article before it is published; a post that still has
unsupported claims after one rewrite is skipped.

Safety limits stop a flood of commands: at least `MIN_SECONDS_BETWEEN_POSTS` (30 s) between posts and at
most `DAILY_POST_LIMIT` (30) posts per rolling 24 hours. Stories that are not mainly about India are
never posted, and a story is never posted twice.

Setup: create a bot with [@BotFather](https://t.me/BotFather) and put its token in
`TELEGRAM_BOT_TOKEN`; start the app, send the bot any message, and it replies with your user id; put
that in `TELEGRAM_OWNER_ID` and restart.

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
5. Stop any copy running on your PC (two copies would fight over the Telegram bot), then in Telegram
   send `status`, `post now`, `start posting`.

Without `TURSO_URL`/`TURSO_TOKEN` the bot uses the local `bot.db` file. Any always-on Linux VM with a
disk also works: create a Python venv, `pip install -r requirements.txt`, and run
`uvicorn app.main:app --host 127.0.0.1 --port 8000` under systemd (`Restart=always`).

## Notes

- Threads allows 250 API posts per 24 hours per profile; text posts are limited to 500 characters.
- Only headlines and short summaries are used; posts are rewritten by the model and always credit
  and link the source. Review each publisher's terms before running at high volume.
- Serving other people (multi-user) requires Meta app review for `threads_content_publish`.
