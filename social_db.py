import logging
import os
from datetime import datetime, timezone

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
    kind TEXT NOT NULL CHECK (kind IN ('club', 'pairing', 'dm')),
    club_id INTEGER UNIQUE REFERENCES clubs(id),
    pairing_id INTEGER UNIQUE REFERENCES pairings(id),
    user_a_id INTEGER REFERENCES users(id),
    user_b_id INTEGER REFERENCES users(id),
    CHECK (
        (kind = 'club' AND club_id IS NOT NULL AND pairing_id IS NULL AND user_a_id IS NULL AND user_b_id IS NULL) OR
        (kind = 'pairing' AND pairing_id IS NOT NULL AND club_id IS NULL AND user_a_id IS NULL AND user_b_id IS NULL) OR
        (kind = 'dm' AND user_a_id IS NOT NULL AND user_b_id IS NOT NULL AND club_id IS NULL AND pairing_id IS NULL)
    )
);

-- The two ALTER statements below are the migration path for a chat_rooms
-- table that already exists in Supabase from before DMs were added --
-- CREATE TABLE IF NOT EXISTS above is a no-op there, so the new columns and
-- widened CHECK/kind constraint have to be applied out-of-band. Both are
-- idempotent (IF NOT EXISTS / DROP+re-ADD) so re-running SCHEMA on every
-- startup, as init_schema() already does, stays safe.
ALTER TABLE chat_rooms ADD COLUMN IF NOT EXISTS user_a_id INTEGER REFERENCES users(id);
ALTER TABLE chat_rooms ADD COLUMN IF NOT EXISTS user_b_id INTEGER REFERENCES users(id);
CREATE UNIQUE INDEX IF NOT EXISTS chat_rooms_dm_pair_idx ON chat_rooms (user_a_id, user_b_id);

DO $$
DECLARE con RECORD;
BEGIN
    FOR con IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'chat_rooms'::regclass AND contype = 'c'
    LOOP
        EXECUTE 'ALTER TABLE chat_rooms DROP CONSTRAINT ' || quote_ident(con.conname);
    END LOOP;
    ALTER TABLE chat_rooms ADD CONSTRAINT chat_rooms_kind_check
        CHECK (kind IN ('club', 'pairing', 'dm'));
    ALTER TABLE chat_rooms ADD CONSTRAINT chat_rooms_shape_check CHECK (
        (kind = 'club' AND club_id IS NOT NULL AND pairing_id IS NULL AND user_a_id IS NULL AND user_b_id IS NULL) OR
        (kind = 'pairing' AND pairing_id IS NOT NULL AND club_id IS NULL AND user_a_id IS NULL AND user_b_id IS NULL) OR
        (kind = 'dm' AND user_a_id IS NOT NULL AND user_b_id IS NOT NULL AND club_id IS NULL AND pairing_id IS NULL)
    );
END $$;

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES chat_rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- race_dedup_key deliberately has no foreign key -- it references a row in
-- races.db, a *separate SQLite database* that's expected to be wiped and
-- rebuilt on every Render redeploy. dedup_key is deterministic (normalized
-- name + date), so the same real-world race keeps the same key across
-- rescrapes and this reference stays valid in the common case. It can orphan
-- if a race is delisted after it happens (harmless) or a source site tweaks
-- a race's name between scrapes (rare, pre-existing dedup fragility, not
-- introduced here). Accepted, not solved -- race_name is stored as a
-- denormalized snapshot so a future "my races" view could still show
-- something meaningful even if the live SQLite row is gone.
CREATE TABLE IF NOT EXISTS race_attendances (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    race_dedup_key TEXT NOT NULL,
    race_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, race_dedup_key)
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


def update_password(user_id: int, password_hash: str) -> None:
    with get_pool().connection() as conn:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))


# -- password reset tokens --------------------------------------------------
# Only the SHA-256 hash of the raw token is ever stored -- a DB leak alone
# can't be used to reset anyone's password, same reasoning as bcrypt for
# login passwords, just with a fast hash since the raw token already has
# 256 bits of entropy (unlike a human-chosen password).

def create_password_reset_token(user_id: int, token_hash: str, expires_at) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_id, token_hash, expires_at),
        )


def get_valid_reset_token(token_hash: str) -> dict | None:
    with get_pool().connection() as conn:
        return _one(
            conn,
            """SELECT * FROM password_reset_tokens
               WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()""",
            (token_hash,),
        )


def consume_reset_token(token_id: int, user_id: int, new_password_hash: str) -> None:
    """Marks the token used and updates the password in one transaction."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = now() WHERE id = %s", (token_id,)
        )
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s", (new_password_hash, user_id)
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


def list_club_members(club_id: int) -> list[dict]:
    with get_pool().connection() as conn:
        return _all(
            conn,
            """SELECT u.id AS user_id, u.display_name, u.city, cm.role
               FROM club_members cm JOIN users u ON u.id = cm.user_id
               WHERE cm.club_id = %s ORDER BY cm.role, u.display_name""",
            (club_id,),
        )


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
    if room["kind"] == "dm":
        return user_id in (room["user_a_id"], room["user_b_id"])
    pairing = get_pairing(room["pairing_id"])
    if pairing is None:
        return False
    return user_id in (pairing["mentee_id"], pairing["mentor_id"])


# -- direct messages -------------------------------------------------------

def get_or_create_dm_room(user_id: int, other_user_id: int) -> int:
    user_a_id, user_b_id = sorted((user_id, other_user_id))
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id FROM chat_rooms WHERE user_a_id = %s AND user_b_id = %s",
            (user_a_id, user_b_id),
        ).fetchone()
        if row is not None:
            return row[0]
        row = conn.execute(
            """INSERT INTO chat_rooms (kind, user_a_id, user_b_id) VALUES ('dm', %s, %s)
               ON CONFLICT (user_a_id, user_b_id) DO UPDATE SET kind = 'dm'
               RETURNING id""",
            (user_a_id, user_b_id),
        ).fetchone()
        return row[0]


def list_user_rooms(user_id: int) -> list[dict]:
    """All rooms (club, dm, pairing) this user belongs to, each with a
    ready-to-link target URL and a preview of the most recent message,
    newest activity first -- powers the /werun/chats inbox."""
    with get_pool().connection() as conn:
        rooms = _all(
            conn,
            """
            SELECT cr.id AS room_id, 'club' AS kind, c.name AS title,
                   '/werun/clubs/' || c.id AS target
            FROM chat_rooms cr
            JOIN clubs c ON c.id = cr.club_id
            JOIN club_members cm ON cm.club_id = c.id
            WHERE cr.kind = 'club' AND cm.user_id = %s

            UNION ALL

            SELECT cr.id AS room_id, 'dm' AS kind, u.display_name AS title,
                   '/werun/dm/' || cr.id AS target
            FROM chat_rooms cr
            JOIN users u ON u.id = (CASE WHEN cr.user_a_id = %s THEN cr.user_b_id ELSE cr.user_a_id END)
            WHERE cr.kind = 'dm' AND (cr.user_a_id = %s OR cr.user_b_id = %s)

            UNION ALL

            SELECT cr.id AS room_id, 'pairing' AS kind,
                   'Mentor chat: ' || u.display_name AS title,
                   '/werun/pairings/' || p.id AS target
            FROM chat_rooms cr
            JOIN pairings p ON p.id = cr.pairing_id
            JOIN users u ON u.id = (CASE WHEN p.mentee_id = %s THEN p.mentor_id ELSE p.mentee_id END)
            WHERE cr.kind = 'pairing' AND (p.mentee_id = %s OR p.mentor_id = %s)
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
        )
        for r in rooms:
            last = _one(
                conn,
                "SELECT body, created_at FROM messages WHERE room_id = %s ORDER BY id DESC LIMIT 1",
                (r["room_id"],),
            )
            r["last_body"] = last["body"] if last else None
            r["last_at"] = last["created_at"] if last else None
        rooms.sort(key=lambda r: r["last_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rooms


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


# -- race attendance (social race-day coordination) -------------------------

def mark_attending(user_id: int, race_dedup_key: str, race_name: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            """INSERT INTO race_attendances (user_id, race_dedup_key, race_name)
               VALUES (%s, %s, %s) ON CONFLICT (user_id, race_dedup_key) DO NOTHING""",
            (user_id, race_dedup_key, race_name),
        )


def unmark_attending(user_id: int, race_dedup_key: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM race_attendances WHERE user_id = %s AND race_dedup_key = %s",
            (user_id, race_dedup_key),
        )


def is_attending(user_id: int, race_dedup_key: str) -> bool:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM race_attendances WHERE user_id = %s AND race_dedup_key = %s",
            (user_id, race_dedup_key),
        ).fetchone()
        return row is not None


def count_attendees(race_dedup_key: str) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM race_attendances WHERE race_dedup_key = %s", (race_dedup_key,)
        ).fetchone()
        return row[0]


def list_attendees(race_dedup_key: str) -> list[dict]:
    with get_pool().connection() as conn:
        return _all(
            conn,
            """SELECT u.id AS user_id, u.display_name, u.city
               FROM race_attendances ra JOIN users u ON u.id = ra.user_id
               WHERE ra.race_dedup_key = %s ORDER BY ra.created_at""",
            (race_dedup_key,),
        )


def get_known_user_ids(user_id: int) -> set[int]:
    """Other users who share a club with this user, or have an active pairing
    with them (either direction). Used to bucket a race's attendee list into
    "people you know" vs everyone else."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT cm2.user_id
               FROM club_members cm1 JOIN club_members cm2
                 ON cm1.club_id = cm2.club_id AND cm2.user_id != cm1.user_id
               WHERE cm1.user_id = %s
               UNION
               SELECT CASE WHEN mentee_id = %s THEN mentor_id ELSE mentee_id END
               FROM pairings
               WHERE status = 'active' AND (mentee_id = %s OR mentor_id = %s)""",
            (user_id, user_id, user_id, user_id),
        ).fetchall()
        return {row[0] for row in rows}
