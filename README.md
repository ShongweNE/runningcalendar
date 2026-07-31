# SA Race Calendar

Aggregates upcoming South African running races (date, location, distances, entry price) from public race-listing sites into one calendar — a web page, a JSON API, and a `.ics` feed you can subscribe to from Google Calendar.

## Data sources

- **runningcalendar.co.za** — scraped HTML (public listing pages, `robots.txt` allows it)
- **startingline.co.za** — its public JSON API at `api.startingline.co.za/api/events`

Both are re-scraped daily. `racepass.com` was investigated but its only working public endpoint returns an unscoped global dataset (8MB+, not filtered to South Africa or to races) rather than a clean listing API, so it was left out — the two sources above already give ~750+ upcoming races.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd.exe
pip install -r requirements.txt

# First scrape (populates races.db) — takes ~20s
python -m scrapers.run_all

# Start the web app
uvicorn main:app --reload
```

Open http://127.0.0.1:8000 for the calendar, http://127.0.0.1:8000/api/races for the raw JSON, and http://127.0.0.1:8000/calendar.ics for the iCalendar feed.

If you don't run `scrapers.run_all` manually first, the app does it automatically on startup when `races.db` is empty.

## Deploy (Render.com, free tier)

1. Push this folder to a GitHub repo.
2. In Render, "New +" → "Blueprint" → point it at the repo. `render.yaml` here defines the web service and start command — Render picks it up automatically.
3. Once deployed, your app is live at `https://<your-service>.onrender.com`.

The app scrapes both sources once daily at 04:00 SAST (`scheduler.py`) and keeps the SQLite DB updated in place — no separate cron job needed. Note: Render's free tier has no persistent disk, so `races.db` resets on each redeploy (not on sleep/wake, just on new deploys) — the app re-scrapes automatically on startup whenever it finds an empty DB, so this self-heals within a minute of a redeploy.

### Keeping it awake

Render's free tier sleeps the app after 15 minutes of no traffic, and a sleeping instance won't run the 04:00 scheduled scrape. `.github/workflows/keepalive.yml` pings the live URL every 10 minutes via GitHub Actions (free, no extra account needed) to keep it awake. If you rename the Render service, update the URL in that file to match. GitHub disables scheduled workflows automatically after 60 days with no commits to the repo — if pings stop, re-enable it from the repo's Actions tab.

## Get races into Google Calendar

In Google Calendar (works the same on the phone app): **Settings → Add calendar → From URL**, paste:

```
https://<your-service>.onrender.com/calendar.ics
```

Google polls subscribed URL calendars every several hours automatically, so new races that get scraped show up in your calendar without you doing anything.

## Project layout

- `scrapers/` — one adapter per source, normalized into the `Race` dataclass (`scrapers/base.py`)
- `db.py` — SQLite storage, upsert + cross-source dedup by normalized name + date
- `scheduler.py` — daily background re-scrape (APScheduler)
- `main.py` — FastAPI app: calendar page, `/api/races`, `/calendar.ics`
- `templates/calendar.html` — FullCalendar.js UI with distance/province filters
