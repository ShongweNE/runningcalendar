import logging
from difflib import SequenceMatcher

from db import upsert_races
from scrapers.base import Race, ScraperAdapter
from scrapers.runningcalendar import RunningCalendarAdapter
from scrapers.startingline import StartingLineAdapter

logger = logging.getLogger(__name__)

ADAPTERS: list[type[ScraperAdapter]] = [RunningCalendarAdapter, StartingLineAdapter]

FUZZY_MATCH_THRESHOLD = 0.85


def run_all() -> list[Race]:
    all_races: list[Race] = []
    for adapter_cls in ADAPTERS:
        adapter = adapter_cls()
        try:
            races = adapter.fetch()
            logger.info("%s: fetched %d races", adapter.source_site, len(races))
            all_races.extend(races)
        except Exception:
            logger.exception("%s: scrape failed, skipping", adapter_cls.source_site)
    _fuzzy_align_names(all_races)
    upsert_races(all_races)
    return all_races


def _fuzzy_align_names(races: list[Race]) -> None:
    """Rewrite near-duplicate names (same date, different source) to match
    exactly, so the exact-match dedup in db.upsert_races collapses them."""
    by_date: dict = {}
    for race in races:
        by_date.setdefault(race.race_date, []).append(race)

    for same_day in by_date.values():
        for i, a in enumerate(same_day):
            for b in same_day[i + 1 :]:
                if a.name == b.name:
                    continue
                ratio = SequenceMatcher(None, a.name.lower(), b.name.lower()).ratio()
                if ratio >= FUZZY_MATCH_THRESHOLD:
                    b.name = a.name


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_all()
