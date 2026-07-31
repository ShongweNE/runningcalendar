# SA Race Calendar + WeRun

Aggregates upcoming South African running races (date, location, distances, entry price) from public race-listing sites into one calendar — a web page, a JSON API, and a `.ics` feed you can subscribe to from Google Calendar.

**WeRun** is a social layer alongside the calendar (new section, login required — the race calendar itself stays public): a user-submitted directory of running clubs/groups with a group chat per club, and a "My First Marathon" tab where first-time runners can request a mentor and experienced runners can offer to pair up, which opens a private 1:1 chat between just those two people.

## Data sources

- **runningcalendar.co.za** — scraped HTML (public listing pages, `robots.txt` allows it)
- **startingline.co.za** — its public JSON API at `api.startingline.co.za/api/events`

Both are re-scraped daily. `racepass.com` was investigated but its only working public endpoint returns an unscoped global dataset (8MB+, not filtered to South Africa or to races) rather than a clean listing API, so it was left out — the two sources above already give ~750+ upcoming races.

## WeRun setup (Supabase Postgres)

Race data lives in a local SQLite file (`races.db`, fine to lose — it re-scrapes). WeRun's user accounts, clubs, and chat can't be recreated the same way, so they live in a separate, free hosted Postgres database (Supabase), which survives Render redeploys.

1. Create a free project at [supabase.com](https://supabase.com).
2. Settings → Database → **Connection pooling** (not the direct connection) → copy the host, port (6543), user, and database name. Your password is the one you set when creating the project.
3. Set these as environment variables (a local `.env` file for dev — already gitignored — or Render's dashboard env vars for deploy):
   ```
   PGHOST=<your-project>.pooler.supabase.com
   PGPORT=6543
   PGUSER=postgres.<your-project-ref>
   PGPASSWORD=<your-db-password>
   PGDATABASE=postgres
   PGSSLMODE=require
   SECRET_KEY=<random string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`>
   ```
   These are passed as separate values rather than one `postgresql://...` URL because Supabase passwords often contain characters (`?`, `+`, `$`, ...) that break URI parsing if not percent-encoded — separate env vars sidestep that entirely.
4. The app refuses to start if `SECRET_KEY` is unset (sessions can't be signed safely without it) and creates its Postgres tables automatically on first startup — no manual schema step needed.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd.exe
pip install -r requirements.txt

# First scrape (populates races.db) — takes ~20s
python -m scrapers.run_all

# Start the web app (reads PGHOST/etc. + SECRET_KEY from .env via python-dotenv)
uvicorn main:app --reload
```

Open http://127.0.0.1:8000 for the calendar, http://127.0.0.1:8000/api/races for the raw JSON, http://127.0.0.1:8000/calendar.ics for the iCalendar feed, and http://127.0.0.1:8000/werun/clubs for WeRun.

If you don't run `scrapers.run_all` manually first, the app does it automatically on startup when `races.db` is empty.

## Deploy (Render.com, free tier)

1. Push this folder to a GitHub repo.
2. In Render, "New +" → "Blueprint" → point it at the repo. `render.yaml` here defines the web service and start command. It declares `SECRET_KEY`/`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` as env vars without values (`sync: false`) so Render prompts you to fill them in during setup rather than storing secrets in the repo — use the same Supabase values from the WeRun setup section above.
3. Once deployed, your app is live at `https://<your-service>.onrender.com`.

The app scrapes both race sources once daily at 04:00 SAST (`scheduler.py`) and keeps the SQLite DB updated in place — no separate cron job needed. Note: Render's free tier has no persistent disk, so `races.db` resets on each redeploy (not on sleep/wake, just on new deploys) — the app re-scrapes automatically on startup whenever it finds an empty DB, so this self-heals within a minute of a redeploy. WeRun's data (accounts, clubs, chat) lives in Supabase instead, specifically so it does **not** get wiped by this.

### Keeping it awake

Render's free tier sleeps the app after 15 minutes of no traffic, and a sleeping instance won't run the 04:00 scheduled scrape. `.github/workflows/keepalive.yml` pings the live URL every 10 minutes via GitHub Actions (free, no extra account needed) to keep it awake, and separately hits `/werun/health` (a real `SELECT 1` against Postgres) — Supabase free projects pause after 7 days of *database* inactivity, which the plain HTTP ping wouldn't prevent on its own. If you rename the Render service, update the URLs in that file to match. GitHub disables scheduled workflows automatically after 60 days with no commits to the repo — if pings stop, re-enable it from the repo's Actions tab.

## Get races into Google Calendar

In Google Calendar (works the same on the phone app): **Settings → Add calendar → From URL**, paste:

```
https://<your-service>.onrender.com/calendar.ics
```

Google polls subscribed URL calendars every several hours automatically, so new races that get scraped show up in your calendar without you doing anything.

## Project layout

- `scrapers/` — one adapter per source, normalized into the `Race` dataclass (`scrapers/base.py`)
- `db.py` — SQLite storage for races, upsert + cross-source dedup by normalized name + date
- `scheduler.py` — daily background re-scrape (APScheduler)
- `main.py` — FastAPI app entrypoint: calendar page, `/api/races`, `/calendar.ics`, session middleware + Postgres pool startup
- `templates/calendar.html` — FullCalendar.js UI with distance/province filters
- `social_db.py` — Postgres (Supabase) connection pool, schema, and all WeRun data access (users, clubs, pairings, chat rooms/messages)
- `auth.py` — password hashing + session helpers (`get_current_user`, `require_user`, `require_room_access`)
- `werun.py` — all WeRun routes (signup/login/profile, clubs, My First Marathon, the shared chat API)
- `templates/base.html` — shared layout + nav (`calendar.html` and everything under `templates/werun/` extend this)
- `templates/werun/` — WeRun pages; `_chat.html` is a shared partial (message list + polling JS) included by both club chat and pairing chat

### Known limitations

WeRun has no email verification, password reset, or report/block/moderation tooling. Since it's meant to help arrange real-world meetups between people who don't already know each other, that's worth addressing before opening it beyond a small trusted circle.
