import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException

from . import alerts, config, drafts, maintenance, safety, state, telegram_bot, threads
from .sources import FEEDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init()
    safety.init_account()  # a new THREADS_USER_ID (a new account) restarts the slow-start limits
    mode = telegram_bot.start()  # runs inside this process: deploy exactly one instance
    maintenance.start()
    if config.ALERT_ON_START:
        commit = os.environ.get("RENDER_GIT_COMMIT", "")[:7] or "local"
        threading.Thread(target=alerts.notify_owner, daemon=True,
                         args=(f"🟢 Bot started ({mode} mode, version {commit}). Send hi for the commands.",)).start()
    yield


app = FastAPI(title="News -> Threads agent", lifespan=lifespan)


@app.api_route("/healthz", methods=["GET", "HEAD"])  # HEAD: uptime monitors use it
def healthz():
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
def status():
    return {
        "categories": list(FEEDS),
        "posts_last_24h": state.posts_in_window(86400)[0],
        "daily_limit": safety.daily_limit_now(),
        "require_approval": config.REQUIRE_APPROVAL,
        "telegram_mode": config.TELEGRAM_MODE,
        "threads_token_days_left": (round(d, 1) if (d := threads.days_left()) is not None else None),
        "drafts_waiting": drafts.count(),
        "dry_run": config.DRY_RUN,
        "recent_posts": [{"url": u, "post_id": p, "category": c} for u, p, c, _ in state.recent_posts()],
    }


@app.post("/telegram/webhook")
def telegram_webhook(update: dict = Body(...), x_telegram_bot_api_secret_token: str = Header(default="")):
    """Receives Telegram's updates in webhook mode (only Telegram knows the secret header)."""
    if config.TELEGRAM_MODE != "webhook" or not hmac.compare_digest(
            x_telegram_bot_api_secret_token.encode(), telegram_bot.webhook_secret().encode()):
        raise HTTPException(403, "forbidden")
    telegram_bot.handle_update(update)
    return {"ok": True}
