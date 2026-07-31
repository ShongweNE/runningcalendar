import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

from scrapers.base import Race, ScraperAdapter

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

PAGE_SIZE = 24
MAX_PAGES = 40  # safety cap so a scraper bug can't loop forever


class RunningCalendarAdapter(ScraperAdapter):
    source_site = "runningcalendar.co.za"
    base_url = "https://runningcalendar.co.za/calendar"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 (compatible; RaceCalendarBot/1.0)"},
            timeout=20,
            follow_redirects=True,
        )

    def fetch(self) -> list[Race]:
        races: list[Race] = []
        page = 1
        while page <= MAX_PAGES:
            resp = self.client.get(
                self.base_url, params={"range": "next-6-months", "page": page}
            )
            resp.raise_for_status()
            page_races, total = parse_calendar_html(resp.text)
            races.extend(page_races)
            if not page_races:
                break
            if total is not None and page * PAGE_SIZE >= total:
                break
            if total is None and len(page_races) < PAGE_SIZE:
                break
            page += 1
        return races


def parse_calendar_html(html: str) -> tuple[list[Race], int | None]:
    """Parse one page of runningcalendar.co.za/calendar into Race objects.

    Returns (races, total_result_count) — total is None if the "Showing X to Y
    of Z results" footer wasn't found (e.g. unexpected page layout).
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("main") or soup

    races: list[Race] = []
    current_year_month: tuple[int, int] | None = None

    for el in container.find_all(["div", "a"]):
        classes = el.get("class") or []
        if el.name == "div" and "group-h" in classes:
            h3 = el.find("h3")
            if h3:
                current_year_month = _parse_group_header(h3.get_text())
            continue
        if el.name == "a" and any(c.startswith("grid-cols-[84px") for c in classes):
            race = _parse_row(el, current_year_month)
            if race:
                races.append(race)

    total = _extract_total(soup)
    return races, total


def _parse_group_header(text: str) -> tuple[int, int] | None:
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower()[:3])
    if not month:
        return None
    return (int(m.group(2)), month)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ·")


def _extract_total(soup: BeautifulSoup) -> int | None:
    m = re.search(r"Showing\s*(\d+)\s*to\s*(\d+)\s*of\s*(\d+)\s*results", soup.get_text())
    return int(m.group(3)) if m else None


def _parse_row(a_tag, year_month: tuple[int, int] | None) -> Race | None:
    url = a_tag.get("href")
    if not url:
        return None

    cols = a_tag.find_all("div", recursive=False)
    if len(cols) < 5:
        return None
    date_col, name_col, place_col, distance_col, price_col = cols[:5]

    day_text = date_col.find_all("div")
    if len(day_text) < 3:
        return None
    month_abbr = day_text[2].get_text(strip=True).lower()[:3]
    day_num = day_text[1].get_text(strip=True)
    month = MONTHS.get(month_abbr)
    if not month or not day_num.isdigit():
        return None

    if year_month and year_month[1] == month:
        year = year_month[0]
    elif year_month:
        # Month header didn't match the row (unexpected) — fall back to header year.
        year = year_month[0]
    else:
        year = date.today().year

    try:
        race_date = date(year, month, int(day_num))
    except ValueError:
        return None

    name_div = name_col.find("div", class_="text-base")
    name = name_div.get_text(strip=True) if name_div else name_col.get_text(strip=True)
    if not name:
        return None

    place_divs = place_col.find_all("div")
    city = _clean(place_divs[0].get_text(" ", strip=True)) if place_divs else None
    province = _clean(place_divs[1].get_text(" ", strip=True)) if len(place_divs) > 1 else None

    distances = [
        span.get_text(strip=True)
        for span in distance_col.find_all("span")
        if span.get_text(strip=True)
    ]

    price_text = price_col.get_text(" ", strip=True)
    price_match = re.search(r"R\s?([\d,]+)", price_text)
    price_from = float(price_match.group(1).replace(",", "")) if price_match else None

    return Race(
        name=name,
        race_date=race_date,
        source_site="runningcalendar.co.za",
        source_url=url,
        distances=distances,
        price_from=price_from,
        city=city,
        province=province,
    )
