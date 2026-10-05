"""Scheduled jobs: reminders 24h before an event starts, and cleanup of unverified data.

Run every 10 minutes in production by deploy/skraning-tasks.timer.
"""

import logging
from datetime import timedelta

import mail
from app import create_app, delete_image, registration_link
from db import from_db, get_db, now_db, registration_state, to_db, utcnow

log = logging.getLogger("skraning.tasks")


def send_reminders() -> int:
    """One email per participant per event, 24h before the event starts, listing their games."""
    db = get_db()
    now = utcnow()
    rows = db.execute(
        """SELECT r.*, q.name AS queue_name, q.starts_at AS queue_starts_at, q.ends_at AS queue_ends_at
           FROM registrations r
           JOIN queues q ON q.id = r.queue_id
           JOIN events e ON e.id = r.event_id
           WHERE e.status = 'published'
             AND r.verified_at IS NOT NULL AND r.cancelled_at IS NULL AND r.reminder_sent_at IS NULL
             AND e.starts_at > ? AND e.starts_at <= ?
           ORDER BY r.event_id, r.email, q.starts_at""",
        (to_db(now), to_db(now + timedelta(hours=24))),
    ).fetchall()

    groups: dict[tuple[int, str], list] = {}
    for reg in rows:
        groups.setdefault((reg["event_id"], reg["email"]), []).append(reg)

    sent = 0
    for (event_id, email), regs in groups.items():
        event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        remind_from = from_db(event["starts_at"]) - timedelta(hours=24)
        # People who registered inside the 24h window just got their confirmation.
        if all(from_db(r["verified_at"]) > remind_from for r in regs):
            ok = True
        else:
            games = [{"reg": r, "link": registration_link(r), **registration_state(db, r)} for r in regs]
            ok = mail.send(email, f"Áminning: {event['name']}", "reminder.txt",
                           name=regs[0]["name"], event=event, games=games)
            sent += ok
        if ok:
            with db:
                db.executemany("UPDATE registrations SET reminder_sent_at = ? WHERE id = ?",
                               [(now_db(), r["id"]) for r in regs])
    return sent


def cleanup() -> None:
    db = get_db()
    now = utcnow()
    day_ago = to_db(now - timedelta(days=1))
    with db:
        db.execute(
            "DELETE FROM registrations WHERE verified_at IS NULL AND cancelled_at IS NULL AND created_at < ?",
            (day_ago,),
        )
        stale = db.execute(
            "SELECT id, image FROM events WHERE status = 'pending' AND created_at < ?",
            (to_db(now - timedelta(days=2)),),
        ).fetchall()
        for event in stale:
            db.execute("DELETE FROM events WHERE id = ?", (event["id"],))
            delete_image(event["image"])
        db.execute("DELETE FROM rate_hits WHERE created_at < ?", (day_ago,))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_app()
    with app.app_context():
        sent = send_reminders()
        cleanup()
    if sent:
        log.info("Sent %d reminder(s)", sent)


if __name__ == "__main__":
    main()
