from datetime import date, datetime, timedelta

from icalendar import Calendar, Event


def build_ics(races: list[dict]) -> str:
    cal = Calendar()
    cal.add("prodid", "-//SA Race Calendar//racecalendar-sa//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "SA Race Calendar")
    cal.add("refresh-interval;value=duration", "PT12H")

    for race in races:
        event = Event()
        event.add("uid", f"{race['source_site']}-{race['name']}-{race['date']}@racecalendar-sa")
        event.add("summary", race["name"])
        race_date = _to_date(race["date"])
        event.add("dtstart", race_date)
        event.add("dtend", race_date + timedelta(days=1))
        event.add("dtstamp", datetime.utcnow())

        location_parts = [p for p in (race.get("city"), race.get("province")) if p]
        if location_parts:
            event.add("location", ", ".join(location_parts))

        description_lines = []
        if race.get("distances"):
            description_lines.append("Distances: " + ", ".join(race["distances"]))
        if race.get("price_from") is not None:
            description_lines.append(f"From R{race['price_from']:.0f}")
        description_lines.append(f"Details: {race['source_url']}")
        event.add("description", "\n".join(description_lines))
        event.add("url", race["source_url"])

        cal.add_component(event)

    return cal.to_ical().decode("utf-8")


def _to_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
