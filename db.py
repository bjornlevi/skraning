"""SQLite access, access codes, time helpers and waitlist logic."""

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA = Path(__file__).with_name("schema.sql")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    con = g.pop("db", None)
    if con is not None:
        con.close()


SCHEMA_VERSION = 3

# Statements that upgrade an existing database from version N to N + 1.
# schema.sql always describes the latest version (used for new databases).
MIGRATIONS: dict[int, list[str]] = {
    1: [  # game runners
        "ALTER TABLE queues ADD COLUMN runner_name TEXT",
        "ALTER TABLE queues ADD COLUMN runner_email TEXT",
        "ALTER TABLE queues ADD COLUMN runner_code_hash TEXT",
        "ALTER TABLE queues ADD COLUMN runner_reminder_sent_at TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS queues_runner_code ON queues(runner_code_hash)",
    ],
    2: [  # payment marker
        "ALTER TABLE registrations ADD COLUMN paid_at TEXT",
    ],
}


def init_db(path: str) -> None:
    """Create a new database, or migrate an older one to SCHEMA_VERSION."""
    con = connect(path)
    try:
        # Take the write lock first: the web workers and the timer may start together.
        con.execute("BEGIN IMMEDIATE")
        (version,) = con.execute("PRAGMA user_version").fetchone()
        (tables,) = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
        if tables == 0:
            con.commit()
            con.executescript(SCHEMA.read_text())
        elif version < SCHEMA_VERSION:
            for v in range(version, SCHEMA_VERSION):
                for statement in MIGRATIONS[v]:
                    con.execute(statement)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.commit()
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} has schema version {version}, newer than this code ({SCHEMA_VERSION})."
            )
        else:
            con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Time — stored as naive UTC text, displayed in the configured timezone
# ---------------------------------------------------------------------------

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def now_db() -> str:
    return to_db(utcnow())


def from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Access codes: 12 chars of Crockford base32 (60 bits), shown as XXXX-XXXX-XXXX.
# Only a SHA-256 hash is stored, so a leaked database does not leak codes.
# ---------------------------------------------------------------------------

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(code: str) -> str:
    code = re.sub(r"[\s-]", "", code or "").upper()
    return code.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def rate_exceeded(key: str, limit: int, window_minutes: int = 60) -> bool:
    since = to_db(utcnow() - timedelta(minutes=window_minutes))
    (count,) = get_db().execute(
        "SELECT COUNT(*) FROM rate_hits WHERE key = ? AND created_at > ?", (key, since)
    ).fetchone()
    return count >= limit


def rate_hit(key: str) -> None:
    db = get_db()
    with db:
        db.execute("INSERT INTO rate_hits (key, created_at) VALUES (?, ?)", (key, now_db()))


def rate_limited(key: str, limit: int, window_minutes: int = 60) -> bool:
    """Record a hit for `key` unless it already exceeds `limit` in the window."""
    if rate_exceeded(key, limit, window_minutes):
        return True
    rate_hit(key)
    return False


# ---------------------------------------------------------------------------
# Queue standing.
#
# Confirmed / waitlisted is never stored; it is computed from the order in
# which registrations were *verified*. Cancellations and capacity changes
# therefore promote people automatically. `last_notified_state` remembers what
# each participant was last told, so changes can be emailed.
# ---------------------------------------------------------------------------


def queue_standing(db: sqlite3.Connection, queue) -> list[dict]:
    """Active registrations in a queue, in order, each with 'state' and 'position'."""
    rows = db.execute(
        """SELECT * FROM registrations
           WHERE queue_id = ? AND verified_at IS NOT NULL AND cancelled_at IS NULL
           ORDER BY verified_at, id""",
        (queue["id"],),
    ).fetchall()
    capacity = queue["capacity"]
    result = []
    for rank, row in enumerate(rows, start=1):
        reg = dict(row)
        if capacity is None or rank <= capacity:
            reg["state"], reg["position"] = "confirmed", rank
        else:
            reg["state"], reg["position"] = "waitlisted", rank - capacity
        result.append(reg)
    return result


def queue_counts(db: sqlite3.Connection, queue) -> dict:
    standing = queue_standing(db, queue)
    confirmed = sum(1 for r in standing if r["state"] == "confirmed")
    return {
        "confirmed": confirmed,
        "waitlisted": len(standing) - confirmed,
        "free": None if queue["capacity"] is None else max(queue["capacity"] - confirmed, 0),
    }


def registration_state(db: sqlite3.Connection, reg) -> dict:
    """{'state': pending|cancelled|confirmed|waitlisted, 'position': int|None}"""
    if reg["cancelled_at"]:
        return {"state": "cancelled", "position": None}
    if not reg["verified_at"]:
        return {"state": "pending", "position": None}
    queue = db.execute("SELECT * FROM queues WHERE id = ?", (reg["queue_id"],)).fetchone()
    for r in queue_standing(db, queue):
        if r["id"] == reg["id"]:
            return {"state": r["state"], "position": r["position"]}
    return {"state": "cancelled", "position": None}


def sync_queue_states(db: sqlite3.Connection, queue_id: int) -> list[dict]:
    """Update last_notified_state for a queue; return registrations whose state changed
    (only those that had been told a previous state)."""
    queue = db.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
    changed = []
    with db:
        for reg in queue_standing(db, queue):
            if reg["state"] != reg["last_notified_state"]:
                db.execute(
                    "UPDATE registrations SET last_notified_state = ? WHERE id = ?",
                    (reg["state"], reg["id"]),
                )
                if reg["last_notified_state"] is not None:
                    changed.append(reg)
    return changed


def overlapping_registration(db: sqlite3.Connection, event_id: int, email: str,
                             starts_at: str, ends_at: str, exclude_queue_id: int = 0):
    """An active (not cancelled) registration by `email` in a game overlapping the interval.

    Intervals are half-open, so a game ending at 17:00 does not overlap one starting at 17:00.
    Pending and waitlisted registrations count too.
    """
    return db.execute(
        """SELECT r.*, q.name AS queue_name, q.starts_at AS queue_starts_at, q.ends_at AS queue_ends_at
           FROM registrations r JOIN queues q ON q.id = r.queue_id
           WHERE r.event_id = ? AND r.email = ? AND r.cancelled_at IS NULL
             AND q.starts_at < ? AND ? < q.ends_at AND q.id != ?
           ORDER BY q.starts_at""",
        (event_id, email, ends_at, starts_at, exclude_queue_id),
    ).fetchone()
