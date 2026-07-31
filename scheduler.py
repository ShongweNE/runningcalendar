import logging

from apscheduler.schedulers.background import BackgroundScheduler

from scrapers.run_all import run_all

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Africa/Johannesburg")
    _scheduler.add_job(_run_scrape_job, "cron", hour=4, minute=0, id="daily_scrape")
    _scheduler.start()
    logger.info("Scheduler started: daily scrape at 04:00 Africa/Johannesburg")
    return _scheduler


def _run_scrape_job() -> None:
    try:
        races = run_all()
        logger.info("Scheduled scrape complete: %d races upserted", len(races))
    except Exception:
        logger.exception("Scheduled scrape job failed")
