import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from scrapers.base import Race

DB_PATH = Path(os.environ.get("RACE_DB_PATH", Path(__file__).parent / "races.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    dedup_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    race_date TEXT NOT NULL,
    distances TEXT NOT NULL,
    price_from REAL,
    city TEXT,
    province TEXT,
    source_site TEXT NOT NULL,
    source_url TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def upsert_races(races: list[Race]) -> None:
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        for race in races:
            key = "|".join(str(part) for part in race.dedup_key())
            existing = conn.execute(
                "SELECT price_from, distances FROM races WHERE dedup_key = ?", (key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO races
                    (dedup_key, name, race_date, distances, price_from, city, province,
                     source_site, source_url, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        race.name,
                        race.race_date.isoformat(),
                        json.dumps(race.distances),
                        race.price_from,
                        race.city,
                        race.province,
                        race.source_site,
                        race.source_url,
                        now,
                        now,
                    ),
                )
            else:
                # Prefer the cheapest known price and union of distances across sources
                merged_distances = sorted(
                    set(json.loads(existing["distances"])) | set(race.distances)
                )
                merged_price = race.price_from
                if existing["price_from"] is not None and race.price_from is not None:
                    merged_price = min(existing["price_from"], race.price_from)
                elif existing["price_from"] is not None:
                    merged_price = existing["price_from"]
                conn.execute(
                    """UPDATE races SET distances = ?, price_from = ?, last_seen = ?
                       WHERE dedup_key = ?""",
                    (json.dumps(merged_distances), merged_price, now, key),
                )
    conn.close()


def get_upcoming_races(from_date: date | None = None) -> list[sqlite3.Row]:
    from_date = from_date or date.today()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM races WHERE race_date >= ? ORDER BY race_date ASC",
        (from_date.isoformat(),),
    ).fetchall()
    conn.close()
    return rows
