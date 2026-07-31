from datetime import date, datetime

import httpx

from scrapers.base import Race, ScraperAdapter

API_URL = "https://api.startingline.co.za/api/events"


class StartingLineAdapter(ScraperAdapter):
    source_site = "startingline.co.za"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 (compatible; RaceCalendarBot/1.0)"},
            timeout=30,
            follow_redirects=True,
        )

    def fetch(self) -> list[Race]:
        resp = self.client.get(API_URL)
        resp.raise_for_status()
        payload = resp.json()
        today = date.today().isoformat()

        races: list[Race] = []
        for event in payload.get("data", []):
            if event.get("status") != "published":
                continue
            start_date = event.get("start_date")
            if not start_date or start_date[:10] < today:
                continue
            race = _to_race(event)
            if race:
                races.append(race)
        return races


def _to_race(event: dict) -> Race | None:
    slug = event.get("slug")
    name = event.get("name")
    start_date = event.get("start_date")
    if not (slug and name and start_date):
        return None
    try:
        race_date = datetime.fromisoformat(start_date.replace("Z", "+00:00")).date()
    except ValueError:
        return None

    distances = []
    prices = []
    for d in event.get("distances") or []:
        if d.get("name"):
            distances.append(d["name"])
        if d.get("price") is not None:
            try:
                prices.append(float(d["price"]))
            except (TypeError, ValueError):
                pass

    return Race(
        name=name,
        race_date=race_date,
        source_site="startingline.co.za",
        source_url=f"https://startingline.co.za/events/{slug}",
        distances=distances,
        price_from=min(prices) if prices else None,
        city=event.get("city"),
        province=event.get("province"),
    )
