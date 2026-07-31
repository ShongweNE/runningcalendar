import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # local dev only: Render sets real env vars directly, this is a no-op there

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import social_db as sdb
from db import get_upcoming_races
from ics_feed import build_ics
from scheduler import start_scheduler
from scrapers.run_all import run_all
from werun import router as werun_router

logger = logging.getLogger(__name__)

try:
    SECRET_KEY = os.environ["SECRET_KEY"]
except KeyError as exc:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. Sessions cannot be signed "
        "safely without it -- generate one (e.g. `python -c \"import secrets; "
        "print(secrets.token_urlsafe(32))\"`) and set it before starting the app."
    ) from exc

# Render sets RENDER=true on every deployed service; only require Secure cookies
# there, since local dev over plain http://127.0.0.1 has no TLS to carry them.
SESSION_HTTPS_ONLY = bool(os.environ.get("RENDER"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not get_upcoming_races():
        threading.Thread(target=run_all, daemon=True).start()
    try:
        start_scheduler()
    except Exception:
        logger.exception("Scheduler failed to start; site still serves existing data")
    try:
        sdb.init_schema()
    except Exception:
        logger.exception("Social DB schema init failed; WeRun features may be unavailable")
    yield
    sdb.close_pool()


app = FastAPI(title="SA Race Calendar", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, https_only=SESSION_HTTPS_ONLY
)
app.include_router(werun_router)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
    current_user = auth.get_current_user(request)
    return templates.TemplateResponse(
        "calendar.html", {"request": request, "current_user": current_user, "active_tab": "races"}
    )


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
