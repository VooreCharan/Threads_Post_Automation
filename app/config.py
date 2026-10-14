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

# Images: "auto" = the article's own image, else a relevant free stock photo, else text only;
# "article" = article image only; "stock" = stock photos only; "off" = text only
IMAGE_MODE = _env("IMAGE_MODE", "auto").lower()
IMAGE_MIN_WIDTH = int(_env("IMAGE_MIN_WIDTH", "600"))  # skip icons, logos and tiny thumbnails
# Free stock-photo APIs (get a free key from each); tried in this order, only those with a key
STOCK_PROVIDERS = [p.strip().lower() for p in _env("STOCK_PROVIDERS", "pexels,pixabay,unsplash").split(",") if p.strip()]
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

# Telegram remote control: bot token from @BotFather, and your own numeric Telegram user id
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_OWNER_ID = _env("TELEGRAM_OWNER_ID")

# Safety limits for the "post <category>" command (protect the account from a flood of commands)
DAILY_POST_LIMIT = int(_env("DAILY_POST_LIMIT", "30"))                    # posts per rolling 24 hours
MIN_SECONDS_BETWEEN_POSTS = int(_env("MIN_SECONDS_BETWEEN_POSTS", "30"))  # pause between two posts
# Comma-separated category names; empty = all categories in sources.py
CATEGORIES = [c.strip() for c in _env("CATEGORIES").split(",") if c.strip()]
# Ignore feed items older than this many hours
MAX_AGE_HOURS = int(_env("MAX_AGE_HOURS", "48"))
DRY_RUN = _env("DRY_RUN").lower() in ("1", "true", "yes")
