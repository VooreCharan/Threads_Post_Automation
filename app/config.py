import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Tried in order when the main model is overloaded (503) or out of quota (429); comma-separated
GEMINI_FALLBACK_MODELS = [m.strip() for m in _env("GEMINI_FALLBACK_MODELS").split(",") if m.strip()]
# Free tier allows ~5 requests/minute per model; keep calls at least this many seconds apart
GEMINI_MIN_INTERVAL = float(_env("GEMINI_MIN_INTERVAL", "13"))

# Order in which text generators are tried. A provider is used only if its API key is set.
# Put the fastest/most reliable first; a provider that fails is skipped for a while (cooldown).
LLM_CHAIN = [p.strip().lower() for p in
             _env("LLM_CHAIN", "groq,nvidia,gemini,openrouter,huggingface,github").split(",") if p.strip()]


def _list(name: str, default: str) -> list[str]:
    return [m.strip() for m in _env(name, default).split(",") if m.strip()]


# OpenAI-compatible providers with free tiers. Free model lists change often: if a name stops
# working, run `python -m app.cli --check-llm` and update the *_MODELS value in .env.
OPENAI_COMPAT = {
    "groq": {"key": _env("GROQ_API_KEY"), "base": "https://api.groq.com/openai/v1",
             "models": _list("GROQ_MODELS", "openai/gpt-oss-20b,openai/gpt-oss-120b,qwen/qwen3.8-27b"),
             "interval": 2.5},
    # NVIDIA build.nvidia.com: free key (needs developer account + phone check), ~40 requests/min total
    "nvidia": {"key": _env("NVIDIA_API_KEY"), "base": "https://integrate.api.nvidia.com/v1",
               "models": _list("NVIDIA_MODELS",
                               "nvidia/nemotron-3.5-lightning-30b-a3b"),
               "interval": 2.0},
    "openrouter": {"key": _env("OPENROUTER_API_KEY"), "base": "https://openrouter.ai/api/v1",
                   "models": _list("OPENROUTER_MODELS",
                                   "nvidia/nemotron-3.5-lightning:free,google/gemma-4-31b-it:free,nvidia/nemotron-3-super-120b-a12b:free"),
                   "interval": 3.5},
    "huggingface": {"key": _env("HF_TOKEN"), "base": "https://router.huggingface.co/v1",
                    "models": _list("HF_MODELS", "meta-llama/Llama-3.3-70B-Instruct,Qwen/Qwen2.5-72B-Instruct"),
                    "interval": 3.0},
    "github": {"key": _env("GITHUB_MODELS_TOKEN"), "base": "https://models.github.ai/inference",
               "models": _list("GITHUB_MODELS", "openai/gpt-4.1-mini"), "interval": 4.0},
}

# Images: the default "auto" = the article's own image first (credited to the publisher, with a link
# back), else a relevant licensed stock photo, else a text post with Threads' own link preview.
# "stock" = only licensed stock photos (never a publisher's photo: the safest for copyright);
# "article" = article image only; "off" = text only. A publisher's photo is their copyright.
IMAGE_MODE = _env("IMAGE_MODE", "auto").lower()
IMAGE_MIN_WIDTH = int(_env("IMAGE_MIN_WIDTH", "600"))  # skip icons, logos and tiny thumbnails
# Free stock-photo APIs (get a free key from each); tried in this order, only those with a key
# Pexels and Pixabay do not require attribution. Unsplash's API terms DO (photographer + Unsplash), so it is
# not a default: add "unsplash" here only if you also show that credit.
STOCK_PROVIDERS = [p.strip().lower() for p in _env("STOCK_PROVIDERS", "pexels,pixabay").split(",") if p.strip()]
PEXELS_API_KEY = _env("PEXELS_API_KEY")
PIXABAY_API_KEY = _env("PIXABAY_API_KEY")
UNSPLASH_ACCESS_KEY = _env("UNSPLASH_ACCESS_KEY")

# Threads API (Meta). See README for how to get these.
THREADS_USER_ID = _env("THREADS_USER_ID")
THREADS_ACCESS_TOKEN = _env("THREADS_ACCESS_TOKEN")  # long-lived token, used to seed the DB

DB_PATH = _env("DB_PATH", "bot.db")
# Optional: keep state in a Turso database instead of a local file (for hosts without a persistent
# disk, e.g. Render free). TURSO_URL looks like libsql://mydb-myorg.turso.io
TURSO_URL = _env("TURSO_URL").replace("libsql://", "https://", 1).rstrip("/")
TURSO_TOKEN = _env("TURSO_TOKEN")

# Only post stories that are mainly about India (events/places in India, Indian government, people,
# companies, markets, sport or culture). Stories mainly about other countries are skipped.
INDIA_ONLY = _env("INDIA_ONLY", "true").lower() in ("1", "true", "yes")

# Background upkeep: every few minutes refresh the news feeds and read the top stories in advance (so a
# "post ..." command answers in seconds), renew the Threads token before it expires and check the database.
# No AI calls are made, so it does not use any free-tier AI quota.
PREWARM = _env("PREWARM", "true").lower() in ("1", "true", "yes")
MAINTENANCE_MINUTES = max(5, int(_env("MAINTENANCE_MINUTES", "12")))
# Send "Bot started" to Telegram on every start (useful to notice restarts on free hosts)
ALERT_ON_START = _env("ALERT_ON_START", "true").lower() in ("1", "true", "yes")

# Telegram: "polling" (default; the app keeps asking Telegram for messages, so the host must stay awake) or
# "webhook" (Telegram calls the app, so a sleeping free host wakes on your message and no keep-alive monitor
# is needed; the first reply after a sleep takes about a minute). Webhook mode needs PUBLIC_URL, which
# Render provides automatically as RENDER_EXTERNAL_URL.
TELEGRAM_MODE = _env("TELEGRAM_MODE", "polling").lower()
PUBLIC_URL = (_env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
TELEGRAM_WEBHOOK_SECRET = _env("TELEGRAM_WEBHOOK_SECRET")  # optional; derived from the bot token if empty

# Telegram remote control: bot token from @BotFather, and your own numeric Telegram user id
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_OWNER_ID = _env("TELEGRAM_OWNER_ID")

# Safety limits for the "post <category>" command (protect the account from looking like spam)
def _daily_limit(raw: str) -> int:
    """Posts allowed per rolling 24 hours (default 15). Never above 240: Threads' own cap is 250 per 24 h."""
    try:
        n = int(raw)
    except ValueError:
        n = 15
    return max(1, min(n, 240))


DAILY_POST_LIMIT = _daily_limit(_env("DAILY_POST_LIMIT", "15"))           # posts per rolling 24 hours
MIN_SECONDS_BETWEEN_POSTS = int(_env("MIN_SECONDS_BETWEEN_POSTS", "300"))  # pause between two posts

# Preview and approve: "post <category>" only prepares a draft in Telegram; nothing is published until
# you tap the "Post to Threads" button. Set to false to publish immediately.
REQUIRE_APPROVAL = _env("REQUIRE_APPROVAL", "true").lower() in ("1", "true", "yes")
DRAFT_TTL_MINUTES = int(_env("DRAFT_TTL_MINUTES", "60"))  # an unapproved draft expires after this long
MAX_PENDING_DRAFTS = int(_env("MAX_PENDING_DRAFTS", "3"))

# Skip stories about violence, sexual crime, suicide, deaths and tragedies: unsuitable for an
# automated account (and for the people involved). Turn off only if you review every draft yourself.
SKIP_SENSITIVE = _env("SKIP_SENSITIVE", "true").lower() in ("1", "true", "yes")

# Article text: how much of the article the AI reads, and the least text a story needs. A story with
# less readable text than MIN_ARTICLE_CHARS is skipped: a post cannot be written or fact-checked from
# a one-line summary.
ARTICLE_CHARS = int(_env("ARTICLE_CHARS", "4500"))
MIN_ARTICLE_CHARS = int(_env("MIN_ARTICLE_CHARS", "250"))
# Comma-separated category names; empty = all categories in sources.py
CATEGORIES = [c.strip() for c in _env("CATEGORIES").split(",") if c.strip()]
# Ignore feed items older than this many hours
MAX_AGE_HOURS = int(_env("MAX_AGE_HOURS", "48"))
DRY_RUN = _env("DRY_RUN").lower() in ("1", "true", "yes")
