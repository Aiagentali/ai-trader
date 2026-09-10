"""ai-trader entry point."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.config.config_loader import load_settings, load_symbols, load_risk
from app.logging_util import setup_logging
from app.database.repository import Repository
from app.market.mt5 import create_feed
from app.ai.client import get_provider
from app.ai.token_budget import TokenBudgetController
from app.risk.engine import RiskEngine
from app.execution.engine import ExecutionEngine
from app.fundamentals.calendar import fetch_calendar
from app.agent.loop import Agent


def main():
    log = setup_logging("ai-trader", os.environ.get("LOG_LEVEL", "INFO"))
    log.info("ai-trader starting")

    settings = load_settings()
    sym_cfg = load_symbols()
    risk_cfg = load_risk()

    symbols = [s for s, v in sym_cfg["symbols"].items() if v.get("enabled", True)]
    log.info(f"symbols: {symbols}")

    mode = settings.get("execution", {}).get("mode", "simulation")
    feed_kind = "mock" if mode in ("simulation", "demo") else "mt5"
    feed = create_feed(seed=42) if feed_kind == "mock" else create_feed("mt5")

    repo = Repository()
    log.info(f"db ready | mode={mode}")

    ai_client = None
    budget = None
    if os.environ.get("AI_API_KEY"):
        ai_client = get_provider()
        budget = TokenBudgetController(repo, settings.get("ai", {}).get("daily_token_limit", 200_000))
        log.info("AI provider connected")
    else:
        log.warning("AI_API_KEY not set — running in data-collection-only mode")

    calendar = fetch_calendar()
    repo.log_event("calendar_loaded", {"count": len(calendar)})

    # Collect news once at startup so the agent has context
    try:
        from app.news.rss import collect
        news_items = collect(min_importance="MEDIUM")
        saved = sum(1 for it in news_items if repo.save_news(it))
        log.info(f"news collected: {len(news_items)} items ({saved} new)")
        repo.log_event("news_collected", {"total": len(news_items), "new": saved})
    except Exception as exc:
        log.warning(f"news collection failed: {exc}")

    risk_engine = RiskEngine(risk_cfg, repo, feed, calendar)
    execution = ExecutionEngine(repo, feed, mode,
                                settings.get("trading", {}).get("live_trading_enabled", False))

    if ai_client and budget:
        agent = Agent(feed, repo, ai_client, budget, risk_engine, execution,
                      symbols, settings)
        agent.run_forever(initial_interval_sec=30)
    else:
        log.info("no AI — data collection mode only. Sleeping.")
        import time
        time.sleep(1)

    repo.close()
    log.info("ai-trader stopped")


if __name__ == "__main__":
    main()
