import json
import threading
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from db import get_upcoming_races
from ics_feed import build_ics
from scheduler import start_scheduler
from scrapers.run_all import run_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not get_upcoming_races():
        threading.Thread(target=run_all, daemon=True).start()
    start_scheduler()
    yield


app = FastAPI(title="SA Race Calendar", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def _row_to_dict(row) -> dict:
    return {
        "name": row["name"],
        "date": row["race_date"],
        "distances": json.loads(row["distances"]),
        "price_from": row["price_from"],
        "city": row["city"],
        "province": row["province"],
        "source_site": row["source_site"],
        "source_url": row["source_url"],
    }


@app.get("/", response_class=None)
def calendar_page(request: Request):
    return templates.TemplateResponse("calendar.html", {"request": request})


@app.get("/api/races")
def api_races(
    distance: str | None = Query(default=None, description="Substring match, e.g. '10km'"),
    province: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
):
    rows = get_upcoming_races(from_date=from_date)
    races = [_row_to_dict(r) for r in rows]

    if distance:
        needle = distance.lower()
        races = [r for r in races if any(needle in d.lower() for d in r["distances"])]
    if province:
        needle = province.lower()
        races = [r for r in races if r["province"] and needle in r["province"].lower()]

    return JSONResponse(races)


@app.get("/calendar.ics")
def calendar_ics():
    rows = get_upcoming_races()
    races = [_row_to_dict(r) for r in rows]
    return PlainTextResponse(build_ics(races), media_type="text/calendar")
