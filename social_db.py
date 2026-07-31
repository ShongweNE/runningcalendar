import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    city TEXT,
    province TEXT,
    experience_level TEXT NOT NULL DEFAULT 'beginner',
    bio TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clubs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    city TEXT,
    province TEXT NOT NULL,
    meeting_schedule TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS club_members (
    club_id INTEGER NOT NULL REFERENCES clubs(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (club_id, user_id)
);

CREATE TABLE IF NOT EXISTS pairings (
    id SERIAL PRIMARY KEY,
    mentee_id INTEGER NOT NULL REFERENCES users(id),
    mentor_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    target_race TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_rooms (
    id SERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('club', 'pairing')),
    club_id INTEGER UNIQUE REFERENCES clubs(id),
    pairing_id INTEGER UNIQUE REFERENCES pairings(id),
    CHECK (
        (kind = 'club' AND club_id IS NOT NULL AND pairing_id IS NULL) OR
        (kind = 'pairing' AND pairing_id IS NOT NULL AND club_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES chat_rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        conninfo = (
            f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
            f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']} "
            f"dbname={os.environ['PGDATABASE']} sslmode={os.environ.get('PGSSLMODE', 'require')}"
        )
        _pool = ConnectionPool(
            conninfo, min_size=1, max_size=5, open=True,
            # Supabase's pooled connection string runs PgBouncer in transaction
            # mode, where the backend connection behind a session can change
            # between statements -- psycopg's default server-side prepared
            # statement caching breaks under that ("prepared statement already
            # exists"). Disable it; every query is re-planned, which is the
            # correct/required trade-off for a pooled connection like this.
            kwargs={"prepare_threshold": None},
        )
    return _pool


def init_schema() -> None:
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)
    logger.info("Social schema ready")


def ping() -> bool:
    """A real round-trip to Postgres, not just a pool health check -- this is
    what keeps Supabase's free-tier project from auto-pausing after 7 days of
    no activity (see the GitHub Actions keepalive workflow)."""
    with get_pool().connection() as conn:
        conn.execute("SELECT 1")
    return True


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# Pooled connections are reused across unrelated calls, so row_factory must
# never be set on the connection itself (it would leak into the next caller
# who checks out that same connection) -- scope it to a cursor instead.
def _one(conn, sql, params=()) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql, params=()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# -- users --------------------------------------------------------------

def create_user(email: str, password_hash: str, display_name: str) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s) RETURNING id",
            (email.lower().strip(), password_hash, display_name),
        ).fetchone()
        return row[0]


def get_user_by_email(email: str) -> dict | None:
    with get_pool().connection() as conn:
        return _one(conn, "SELECT * FROM users WHERE email = %s", (email.lower().strip(),))


def get_user_by_id(user_id: int) -> dict | None:
    with get_pool().connection() as conn:
        return _one(conn, "SELECT * FROM users WHERE id = %s", (user_id,))


def update_profile(user_id: int, display_name: str, city: str | None, province: str | None,
                    experience_level: str, bio: str | None) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            """UPDATE users SET display_name=%s, city=%s, province=%s,
               experience_level=%s, bio=%s WHERE id=%s""",
            (display_name, city, province, experience_level, bio, user_id),
        )


# -- clubs ----------------------------------------------------------------

def create_club(name: str, description: str | None, city: str | None, province: str,
                 meeting_schedule: str | None, created_by: int) -> int:
    with get_pool().connection() as conn:
        club_id = conn.execute(
            """INSERT INTO clubs (name, description, city, province, meeting_schedule, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, description, city, province, meeting_schedule, created_by),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chat_rooms (kind, club_id) VALUES ('club', %s)", (club_id,)
        )
        conn.execute(
            "INSERT INTO club_members (club_id, user_id, role) VALUES (%s, %s, 'owner')",
            (club_id, created_by),
        )
        return club_id


def list_clubs(province: str | None = None) -> list[dict]:
    with get_pool().connection() as conn:
        if province:
            return _all(conn, "SELECT * FROM clubs WHERE province = %s ORDER BY name", (province,))
        return _all(conn, "SELECT * FROM clubs ORDER BY name")


def get_club(club_id: int) -> dict | None:
    with get_pool().connection() as conn:
        return _one(conn, "SELECT * FROM clubs WHERE id = %s", (club_id,))


def join_club(club_id: int, user_id: int) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            """INSERT INTO club_members (club_id, user_id) VALUES (%s, %s)
               ON CONFLICT (club_id, user_id) DO NOTHING""",
            (club_id, user_id),
        )


def is_club_member(club_id: int, user_id: int) -> bool:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM club_members WHERE club_id=%s AND user_id=%s", (club_id, user_id)
        ).fetchone()
        return row is not None


def get_club_chat_room_id(club_id: int) -> int | None:
    with get_pool().connection() as conn:
        row = conn.execute("SELECT id FROM chat_rooms WHERE club_id = %s", (club_id,)).fetchone()
        return row[0] if row else None


# -- pairings (My First Marathon) -----------------------------------------

def create_pairing_request(mentee_id: int, target_race: str | None, note: str | None) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO pairings (mentee_id, target_race, note) VALUES (%s, %s, %s)
               RETURNING id""",
            (mentee_id, target_race, note),
        ).fetchone()
        return row[0]


def list_open_pairing_requests() -> list[dict]:
    with get_pool().connection() as conn:
        return _all(
            conn,
            """SELECT p.*, u.display_name AS mentee_name, u.city AS mentee_city, u.province AS mentee_province
               FROM pairings p JOIN users u ON u.id = p.mentee_id
               WHERE p.status = 'pending' ORDER BY p.created_at""",
        )


def get_pairing(pairing_id: int) -> dict | None:
    with get_pool().connection() as conn:
        return _one(conn, "SELECT * FROM pairings WHERE id = %s", (pairing_id,))


def accept_pairing(pairing_id: int, mentor_id: int) -> bool:
    """Returns False if the request was already taken/isn't pending (race-safe)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            """UPDATE pairings SET mentor_id = %s, status = 'active'
               WHERE id = %s AND status = 'pending' RETURNING id""",
            (mentor_id, pairing_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "INSERT INTO chat_rooms (kind, pairing_id) VALUES ('pairing', %s)", (pairing_id,)
        )
        return True


def get_pairing_chat_room_id(pairing_id: int) -> int | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id FROM chat_rooms WHERE pairing_id = %s", (pairing_id,)
        ).fetchone()
        return row[0] if row else None


def list_my_pairings(user_id: int) -> list[dict]:
    with get_pool().connection() as conn:
        return _all(
            conn,
            """SELECT * FROM pairings WHERE mentee_id = %s OR mentor_id = %s
               ORDER BY created_at DESC""",
            (user_id, user_id),
        )


# -- chat rooms / messages -------------------------------------------------

def get_chat_room(room_id: int) -> dict | None:
    with get_pool().connection() as conn:
        return _one(conn, "SELECT * FROM chat_rooms WHERE id = %s", (room_id,))


def user_can_access_room(user_id: int, room_id: int) -> bool:
    room = get_chat_room(room_id)
    if room is None:
        return False
    if room["kind"] == "club":
        return is_club_member(room["club_id"], user_id)
    pairing = get_pairing(room["pairing_id"])
    if pairing is None:
        return False
    return user_id in (pairing["mentee_id"], pairing["mentor_id"])


def add_message(room_id: int, user_id: int, body: str) -> dict:
    with get_pool().connection() as conn:
        return _one(
            conn,
            """INSERT INTO messages (room_id, user_id, body) VALUES (%s, %s, %s)
               RETURNING id, room_id, user_id, body, created_at""",
            (room_id, user_id, body),
        )


def list_messages(room_id: int, since_id: int = 0) -> list[dict]:
    with get_pool().connection() as conn:
        return _all(
            conn,
            """SELECT m.id, m.room_id, m.user_id, u.display_name, m.body, m.created_at
               FROM messages m JOIN users u ON u.id = m.user_id
               WHERE m.room_id = %s AND m.id > %s ORDER BY m.id""",
            (room_id, since_id),
        )
