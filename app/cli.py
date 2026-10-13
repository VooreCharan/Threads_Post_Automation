"""Run the agent from the command line:  python -m app.cli --dry-run --categories health,technology"""
import argparse
import json
import logging

from . import bot, state
from .sources import FEEDS


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="News -> Threads agent")
    p.add_argument("--categories", default="", help=f"comma list from: {', '.join(FEEDS)}")
    p.add_argument("--count", type=int, default=0, help="posts this run (default POSTS_PER_RUN)")
    p.add_argument("--dry-run", action="store_true", help="generate posts but do not publish")
    p.add_argument("--check-llm", action="store_true", help="test every configured AI provider/model")
    args = p.parse_args()

    if args.check_llm:
        from . import llm
        slots = llm.build_slots()
        if not slots:
            print("No provider has an API key set. Add GEMINI_API_KEY and/or GROQ_API_KEY etc. to .env")
        for slot, ok, msg, secs in llm.check_slots():
            print(f"{'OK  ' if ok else 'FAIL'} {str(slot):55} {secs:5.1f}s  {'' if ok else msg}")
        return

    state.init()
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    results = bot.run_once(cats or None, args.count or None, True if args.dry_run else None)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
