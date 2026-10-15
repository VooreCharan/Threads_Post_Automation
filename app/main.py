import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config, drafts, safety, state, telegram_bot
from .sources import FEEDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init()
    safety.init_account()  # a new THREADS_USER_ID (a new account) restarts the slow-start limits
    telegram_bot.start_polling_thread()  # runs inside this process: deploy exactly one instance
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
        "drafts_waiting": drafts.count(),
        "dry_run": config.DRY_RUN,
        "recent_posts": [{"url": u, "post_id": p, "category": c} for u, p, c, _ in state.recent_posts()],
    }
