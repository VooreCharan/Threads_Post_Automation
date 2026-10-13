import hmac
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from . import bot, config, state, telegram_bot
from .sources import FEEDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init()
    # Runs inside this process: deploy exactly one instance.
    # The heartbeat only posts while posting is ON (toggle with Telegram "start posting"/"stop posting").
    scheduler.add_job(lambda: bot.tick(telegram_bot.notify), "interval", seconds=10,
                      max_instances=1, coalesce=True)
    scheduler.start()
    telegram_bot.start_polling_thread()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="News -> Threads agent", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def status():
    return {
        "categories": list(FEEDS),
        "posting_enabled": bot.posting_enabled(),
        "gap_seconds": [config.POST_GAP_MIN_SECONDS, config.POST_GAP_MAX_SECONDS],
        "dry_run": config.DRY_RUN,
        "recent_posts": [{"url": u, "post_id": p, "category": c} for u, p, c, _ in state.recent_posts()],
    }


@app.post("/api/trigger")
def trigger(background: BackgroundTasks, categories: str = "", count: int = 0, dry_run: bool = False,
            x_trigger_key: str = Header(default="")):
    """Run the agent now. Header X-Trigger-Key must match TRIGGER_SECRET.
    Optional query: ?categories=health,technology&count=2&dry_run=true"""
    if not config.TRIGGER_SECRET or not hmac.compare_digest(x_trigger_key, config.TRIGGER_SECRET):
        raise HTTPException(401, "Bad trigger key")
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    unknown = [c for c in cats if c not in FEEDS]
    if unknown:
        raise HTTPException(400, f"Unknown categories: {unknown}. Available: {list(FEEDS)}")
    background.add_task(bot.run_once, cats or None, max(0, min(count, 5)) or None, dry_run or None)
    return {"started": True}
