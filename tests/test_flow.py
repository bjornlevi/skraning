"""End-to-end tests of the main flows, using the in-memory mail backend."""

import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mail  # noqa: E402
from app import create_app, render_markdown  # noqa: E402
from db import get_db, to_db, utcnow  # noqa: E402

CODE_RE = re.compile(r"\b[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b")
LINK_RE = re.compile(r"https?://\S+")


class FlowTest(unittest.TestCase):
    prefix = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "PREFIX": self.prefix,
            "DATABASE": f"{self.tmp.name}/test.db",
            "UPLOAD_DIR": f"{self.tmp.name}/uploads",
            "BASE_URL": f"https://example.org{self.prefix}",
            "MAIL_BACKEND": "memory",
            "SECRET_KEY": "test",
        })
        mail.outbox.clear()
        self.organizer = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def url(self, path):
        return self.prefix + path

    def post(self, client, path, data=None, **kw):
        client.get(self.url("/"))  # ensure a CSRF token exists in the session
        with client.session_transaction() as s:
            token = s["csrf"]
        return client.post(self.url(path), data={**(data or {}), "csrf": token}, **kw)

    def last_mail(self, to):
        msgs = [m for m in mail.outbox if m["To"] == to]
        self.assertTrue(msgs, f"no mail to {to}")
        return msgs[-1]

    def code_from(self, msg):
        return CODE_RE.search(msg.get_content()).group(0)

    def create_event(self, **extra):
        self.day = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        data = {"name": "Spilamót", "date": self.day, "time": "10:00", "end_time": "23:00",
                "location": "Hlöðuloftið", "description": "Gaman", "listed": "1",
                "organizer_name": "Umsjón",
                "organizer_email": "org@example.org", **extra}
        resp = self.post(self.organizer, "/new", data)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        code = self.code_from(self.last_mail("org@example.org"))
        resp = self.organizer.get(self.url(f"/c/{code}"))
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM events").fetchone())

    def add_queue(self, event, name="Kall Cthulhu", capacity="1", start="13:00", end="17:00",
                  expect=302, **extra):
        resp = self.post(self.organizer, f"/admin/{event['slug']}/queues/new",
                         {"name": name, "capacity": capacity, "is_open": "1", "sort_order": "1",
                          "date": self.day, "start_time": start, "end_time": end, **extra})
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[-3000:])
        if expect != 302:
            return resp
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM queues WHERE name = ?", (name,)).fetchone())

    def register(self, event, queue, email, verify=True):
        client = self.app.test_client()
        resp = self.post(client, f"/e/{event['slug']}/register",
                         {"name": email.split("@")[0], "email": email, "queue_id": str(queue["id"])})
        if resp.status_code != 302:
            return client, resp
        if verify:
            code = self.code_from(self.last_mail(email))
            resp = client.get(self.url(f"/c/{code}"), follow_redirects=True)
        return client, resp

    # -- tests ---------------------------------------------------------------

    def test_event_creation_and_verification(self):
        event = self.create_event()
        self.assertEqual(event["status"], "published")
        resp = self.organizer.get(self.url(f"/admin/{event['slug']}"))
        self.assertEqual(resp.status_code, 200)
        # Other browsers cannot see the admin page without the code
        self.assertEqual(self.app.test_client().get(self.url(f"/admin/{event['slug']}")).status_code, 403)
        # Public page and home listing
        self.assertIn("Spilamót", self.app.test_client().get(self.url("/")).get_data(as_text=True))

    def test_unverified_event_is_hidden(self):
        start = datetime.now() + timedelta(days=3)
        self.post(self.organizer, "/new", {
            "name": "Leynd", "date": start.strftime("%Y-%m-%d"), "time": "10:00", "end_time": "12:00",
            "organizer_name": "X", "organizer_email": "x@example.org"})
        with self.app.app_context():
            slug = get_db().execute("SELECT slug FROM events").fetchone()["slug"]
        self.assertEqual(self.organizer.get(self.url(f"/e/{slug}")).status_code, 404)

    def test_waitlist_and_promotion(self):
        event = self.create_event()
        queue = self.add_queue(event, capacity="1")
        a, resp_a = self.register(event, queue, "a@example.org")
        self.assertIn("Þú átt pláss", resp_a.get_data(as_text=True))
        b, resp_b = self.register(event, queue, "b@example.org")
        self.assertIn("Biðlisti — númer 1", resp_b.get_data(as_text=True))

        # A cancels -> B promoted and emailed
        with self.app.app_context():
            a_id = get_db().execute("SELECT id FROM registrations WHERE email = 'a@example.org'").fetchone()[0]
        self.post(a, f"/me/{a_id}/cancel", {"confirm": "1"})
        promo = self.last_mail("b@example.org")
        self.assertIn("Þú fékkst pláss", promo["Subject"])
        # The signed link in the promotion email opens B's status in a fresh browser
        link = LINK_RE.search(promo.get_content()).group(0).removeprefix("https://example.org")
        resp = self.app.test_client().get(link, follow_redirects=True)
        self.assertIn("Þú átt pláss", resp.get_data(as_text=True))

    def test_capacity_increase_promotes(self):
        event = self.create_event()
        queue = self.add_queue(event, capacity="1")
        self.register(event, queue, "a@example.org")
        self.register(event, queue, "b@example.org")
        mail.outbox.clear()
        self.post(self.organizer, f"/admin/{event['slug']}/queues/{queue['id']}/edit",
                  {"name": queue["name"], "capacity": "2", "is_open": "1",
                   "date": self.day, "start_time": "13:00", "end_time": "17:00"})
        self.assertIn("Þú fékkst pláss", self.last_mail("b@example.org")["Subject"])

    def test_unverified_registration_holds_no_spot(self):
        event = self.create_event()
        queue = self.add_queue(event, capacity="1")
        self.register(event, queue, "slow@example.org", verify=False)
        _, resp = self.register(event, queue, "fast@example.org")
        self.assertIn("Þú átt pláss", resp.get_data(as_text=True))

    def test_max_per_person(self):
        event = self.create_event(max_per_person="1")
        q1 = self.add_queue(event, name="Spil 1", start="10:00", end="14:00")
        q2 = self.add_queue(event, name="Spil 2", start="15:00", end="19:00")
        self.register(event, q1, "a@example.org")
        _, resp = self.register(event, q2, "a@example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mest 1 spil", resp.get_data(as_text=True))

    def test_overlapping_games_rejected(self):
        event = self.create_event()
        morning = self.add_queue(event, name="Morgunn A", start="10:00", end="14:00")
        morning_b = self.add_queue(event, name="Morgunn B", start="10:00", end="14:00")
        long_game = self.add_queue(event, name="Langt", start="12:00", end="18:00")
        afternoon = self.add_queue(event, name="Síðdegi", start="14:00", end="18:00")

        client, resp = self.register(event, morning, "a@example.org")
        self.assertEqual(resp.status_code, 200)
        # Same slot and partially overlapping games are refused
        for q in (morning_b, long_game):
            _, resp = self.register(event, q, "a@example.org")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("skarast", resp.get_data(as_text=True))
        # Back-to-back is fine (14:00 end, 14:00 start)
        _, resp = self.register(event, afternoon, "a@example.org")
        self.assertIn("Þú átt pláss", resp.get_data(as_text=True))
        # Event page marks clashing games for this browser
        html = client.get(self.url(f"/e/{event['slug']}")).get_data(as_text=True)
        self.assertIn("Skarast á við „Morgunn A“", html)

    def test_overlap_counts_unverified_and_waitlisted(self):
        event = self.create_event()
        full = self.add_queue(event, name="Fullt", capacity="1", start="10:00", end="14:00")
        other = self.add_queue(event, name="Annað", start="10:00", end="14:00")
        self.register(event, full, "first@example.org")
        self.register(event, full, "a@example.org")          # waitlisted
        self.register(event, other, "b@example.org", verify=False)
        for email in ("a@example.org",):
            _, resp = self.register(event, other, email)
            self.assertEqual(resp.status_code, 400)
        _, resp = self.register(event, full, "b@example.org")  # b's unverified 'other' blocks
        self.assertEqual(resp.status_code, 400)

    def test_cancel_frees_time_slot(self):
        event = self.create_event()
        q1 = self.add_queue(event, name="A", start="10:00", end="14:00")
        q2 = self.add_queue(event, name="B", start="10:00", end="14:00")
        client, _ = self.register(event, q1, "a@example.org")
        with self.app.app_context():
            reg_id = get_db().execute("SELECT id FROM registrations").fetchone()[0]
        self.post(client, f"/me/{reg_id}/cancel", {"confirm": "1"})
        _, resp = self.register(event, q2, "a@example.org")
        self.assertIn("Þú átt pláss", resp.get_data(as_text=True))

    def test_game_must_be_within_event(self):
        event = self.create_event()
        resp = self.add_queue(event, name="Of seint", start="22:00", end="23:30", expect=400)
        self.assertIn("innan viðburðarins", resp.get_data(as_text=True))
        resp = self.add_queue(event, name="Núll", start="15:00", end="15:00", expect=400)
        self.assertIn("ljúka eftir", resp.get_data(as_text=True))
        resp = self.add_queue(event, name="AM/PM", start="3pm", end="5pm", expect=400)
        self.assertIn("13:00", resp.get_data(as_text=True))

    def test_24h_time_formats(self):
        event = self.create_event(end_date=(datetime.now() + timedelta(days=31)).strftime("%Y-%m-%d"),
                                  end_time="04:00")
        for i, (start, end) in enumerate([("13:00", "17:00"), ("13.00", "17.30"), ("1300", "1700"), ("10", "12")]):
            self.add_queue(event, name=f"Snið {i}", start=start, end=end)
        night = self.add_queue(event, name="Nótt", start="22:00", end="02:00")
        with self.app.app_context():
            from app import fmt_range
            rows = get_db().execute("SELECT name, starts_at, ends_at FROM queues ORDER BY id").fetchall()
            ranges = {r["name"]: fmt_range(r["starts_at"], r["ends_at"]) for r in rows}
        self.assertTrue(ranges["Snið 1"].endswith("kl. 13:00–17:30"), ranges)
        self.assertTrue(ranges["Snið 3"].endswith("kl. 10:00–12:00"), ranges)
        # Ends the next day
        self.assertRegex(ranges["Nótt"], r"kl\. 22:00 – \w+ \d+\. \w+ \d{4} kl\. 02:00$")
        self.assertNotIn("PM", self.organizer.get(self.url(f"/e/{event['slug']}")).get_data(as_text=True))

    def test_event_cannot_shrink_around_games(self):
        event = self.create_event()
        self.add_queue(event, name="Kvöld", start="19:00", end="22:00")
        resp = self.post(self.organizer, f"/admin/{event['slug']}/edit",
                         {"name": "Spilamót", "date": self.day, "time": "10:00", "end_time": "18:00"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("„Kvöld“", resp.get_data(as_text=True))

    def test_registration_closes_when_game_starts(self):
        event = self.create_event()
        queue = self.add_queue(event)
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE events SET starts_at = ?", (to_db(utcnow() - timedelta(hours=1)),))
                db.execute("UPDATE queues SET starts_at = ?", (to_db(utcnow() - timedelta(minutes=5)),))
        _, resp = self.register(event, queue, "late@example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Hafið", resp.get_data(as_text=True))

    def test_lost_code_rotates_codes(self):
        event = self.create_event()
        queue = self.add_queue(event)
        self.register(event, queue, "a@example.org")
        old_code = self.code_from(self.last_mail("a@example.org"))
        client = self.app.test_client()
        self.post(client, "/lost", {"email": "a@example.org"})
        new_code = self.code_from(self.last_mail("a@example.org"))
        self.assertNotEqual(old_code, new_code)
        self.assertEqual(client.get(self.url(f"/c/{old_code}")).status_code, 404)
        self.assertEqual(client.get(self.url(f"/c/{new_code}")).status_code, 302)

    def test_csrf_required(self):
        event = self.create_event()
        resp = self.organizer.post(self.url(f"/admin/{event['slug']}/queues/new"), data={"name": "X"})
        self.assertEqual(resp.status_code, 400)

    def test_reminders_sent_once(self):
        import tasks

        event = self.create_event()
        q1 = self.add_queue(event, name="Fyrra", capacity="5", start="10:00", end="14:00")
        q2 = self.add_queue(event, name="Seinna", capacity="5", start="15:00", end="19:00")
        self.register(event, q1, "a@example.org")
        self.register(event, q2, "a@example.org")
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE events SET starts_at = ?", (to_db(utcnow() + timedelta(hours=20)),))
                db.execute("UPDATE registrations SET verified_at = ?", (to_db(utcnow() - timedelta(days=2)),))
            mail.outbox.clear()
            self.assertEqual(tasks.send_reminders(), 1)  # one email listing both games
            self.assertEqual(tasks.send_reminders(), 0)
        self.assertEqual(len(mail.outbox), 1)
        msg = self.last_mail("a@example.org")
        self.assertIn("Áminning", msg["Subject"])
        self.assertIn("Fyrra", msg.get_content())
        self.assertIn("Seinna", msg.get_content())
        self.assertIn(f"https://example.org{self.prefix}/l/r/", msg.get_content())

    def test_password_required_to_create(self):
        self.app.config["CREATE_PASSWORD"] = "drekar"
        day = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        data = {"name": "X", "date": day, "time": "10:00", "end_time": "12:00",
                "organizer_name": "A", "organizer_email": "a@example.org"}
        self.assertIn('name="create_password"', self.organizer.get(self.url("/new")).get_data(as_text=True))
        resp = self.post(self.organizer, "/new", {**data, "create_password": "rangt"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Rangt lykilorð", resp.get_data(as_text=True))
        self.assertFalse(mail.outbox)
        resp = self.post(self.organizer, "/new", {**data, "create_password": "drekar"})
        self.assertEqual(resp.status_code, 302)

    def test_production_requires_password(self):
        with self.assertRaises(RuntimeError):
            create_app({"MAIL_BACKEND": "smtp", "SECRET_KEY": "s", "CREATE_PASSWORD": "",
                        "DATABASE": f"{self.tmp.name}/p.db"})

    def test_weekday_case(self):
        from app import fmt_dt
        with self.app.app_context():
            value = "2026-10-06 12:00:00"  # a Tuesday, 12:00 UTC = 12:00 in Reykjavík
            self.assertEqual(fmt_dt(value), "þriðjudagur 6. október 2026 kl. 12:00")
            self.assertEqual(fmt_dt(value, accusative=True), "þriðjudaginn 6. október 2026 kl. 12:00")

    # -- game runners ----------------------------------------------------------

    def queue_row(self, queue_id):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone())

    def edit_queue_as_organizer(self, event, queue, **changes):
        data = {"name": queue["name"], "capacity": str(queue["capacity"] or ""), "is_open": "1",
                "date": self.day, "start_time": "13:00", "end_time": "17:00",
                "runner_name": queue["runner_name"] or "", "runner_email": queue["runner_email"] or "",
                **changes}
        return self.post(self.organizer, f"/admin/{event['slug']}/queues/{queue['id']}/edit", data)

    def runner_client(self, email):
        client = self.app.test_client()
        code = self.code_from(self.last_mail(email))
        resp = client.get(self.url(f"/c/{code}"))
        self.assertEqual(resp.status_code, 302)
        return client, code

    def test_runner_invited_and_edits_allowed_fields(self):
        event = self.create_event()
        queue = self.add_queue(event, runner_name="Gunna", runner_email="gm@example.org")
        invite = self.last_mail("gm@example.org")
        self.assertIn("Þú stjórnar", invite["Subject"])
        self.register(event, queue, "p1@example.org")

        gm, _ = self.runner_client("gm@example.org")
        html = gm.get(self.url(f"/game/{queue['id']}")).get_data(as_text=True)
        self.assertIn("p1@example.org", html)  # sees players

        resp = self.post(gm, f"/game/{queue['id']}", {
            "name": "Nýtt nafn", "description": "Ný lýsing", "capacity": "4",
            # Not editable by runners; must be ignored
            "date": self.day, "start_time": "10:00", "end_time": "11:00", "is_open": "",
            "runner_email": "hijack@example.org"})
        self.assertEqual(resp.status_code, 302)
        row = self.queue_row(queue["id"])
        self.assertEqual((row["name"], row["description"], row["capacity"]), ("Nýtt nafn", "Ný lýsing", 4))
        self.assertEqual((row["starts_at"], row["ends_at"], row["is_open"], row["runner_email"]),
                         (queue["starts_at"], queue["ends_at"], 1, "gm@example.org"))

        # No access to the organizer's pages or other games
        self.assertEqual(gm.get(self.url(f"/admin/{event['slug']}")).status_code, 403)
        other = self.add_queue(event, name="Annað spil")
        self.assertEqual(gm.get(self.url(f"/game/{other['id']}")).status_code, 403)

    def test_runner_capacity_increase_promotes(self):
        event = self.create_event()
        queue = self.add_queue(event, capacity="1", runner_email="gm@example.org")
        gm, _ = self.runner_client("gm@example.org")
        self.register(event, queue, "a@example.org")
        self.register(event, queue, "b@example.org")
        self.post(gm, f"/game/{queue['id']}", {"name": queue["name"], "capacity": "2"})
        self.assertIn("Þú fékkst pláss", self.last_mail("b@example.org")["Subject"])

    def test_changing_runner_revokes_old_runner(self):
        event = self.create_event()
        queue = self.add_queue(event, runner_email="old@example.org")
        old, old_code = self.runner_client("old@example.org")
        self.edit_queue_as_organizer(event, self.queue_row(queue["id"]), runner_email="new@example.org")

        self.assertEqual(old.get(self.url(f"/game/{queue['id']}")).status_code, 403)
        self.assertEqual(self.app.test_client().get(self.url(f"/c/{old_code}")).status_code, 404)
        new, _ = self.runner_client("new@example.org")
        self.assertEqual(new.get(self.url(f"/game/{queue['id']}")).status_code, 200)

        # Removing the runner revokes access too
        self.edit_queue_as_organizer(event, self.queue_row(queue["id"]), runner_email="", runner_name="")
        self.assertEqual(new.get(self.url(f"/game/{queue['id']}")).status_code, 403)

    def test_runner_name_shown_publicly(self):
        event = self.create_event()
        self.add_queue(event, runner_name="Gunna Spilastjóri", runner_email="gm@example.org")
        html = self.app.test_client().get(self.url(f"/e/{event['slug']}")).get_data(as_text=True)
        self.assertIn("Stjórnandi: Gunna Spilastjóri", html)
        self.assertNotIn("gm@example.org", html)

    def test_lost_code_includes_runner(self):
        event = self.create_event()
        queue = self.add_queue(event, runner_email="gm@example.org")
        self.post(self.app.test_client(), "/lost", {"email": "gm@example.org"})
        client, _ = self.runner_client("gm@example.org")
        self.assertEqual(client.get(self.url(f"/game/{queue['id']}")).status_code, 200)

    def test_runner_reminder(self):
        import tasks

        event = self.create_event()
        q1 = self.add_queue(event, name="Fyrra", capacity="1", start="10:00", end="14:00",
                            runner_name="Gunna", runner_email="gm@example.org")
        q2 = self.add_queue(event, name="Seinna", capacity="5", start="15:00", end="19:00",
                            runner_name="Gunna", runner_email="gm@example.org")
        self.register(event, q1, "a@example.org")
        self.register(event, q1, "b@example.org")  # waitlisted
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE events SET starts_at = ?", (to_db(utcnow() + timedelta(hours=20)),))
            mail.outbox.clear()
            self.assertEqual(tasks.send_runner_reminders(), 1)  # one email for both games
            self.assertEqual(tasks.send_runner_reminders(), 0)
        body = self.last_mail("gm@example.org").get_content()
        for text in ("Fyrra", "Seinna", "a@example.org", "Á biðlista", "b@example.org", "Enginn skráður"):
            self.assertIn(text, body)

    def test_cancelling_event_notifies_runner(self):
        event = self.create_event()
        self.add_queue(event, runner_email="gm@example.org")
        self.post(self.organizer, f"/admin/{event['slug']}/cancel", {"confirm": "1"})
        body = self.last_mail("gm@example.org").get_content()
        self.assertIn("sem þú áttir að stjórna", body)

    def test_game_link_preselects_game(self):
        event = self.create_event()
        first = self.add_queue(event, name="Fyrra", start="13:00", end="17:00")
        second = self.add_queue(event, name="Seinna", start="18:00", end="22:00")
        link = f"/e/{event['slug']}/{second['id']}"
        self.assertIn(link, self.organizer.get(self.url(f"/admin/{event['slug']}")).get_data(as_text=True))
        page = self.app.test_client().get(self.url(link)).get_data(as_text=True)
        self.assertRegex(page, rf'value="{second["id"]}"[^>]*checked')
        self.assertNotRegex(page, rf'value="{first["id"]}"[^>]*checked')
        self.assertIn("<title>Seinna – Spilamót", page)
        # A game from another event, or one that doesn't exist, is not found
        self.assertEqual(self.app.test_client().get(self.url(f"/e/{event['slug']}/9999")).status_code, 404)

    def test_payment_toggle(self):
        event = self.create_event()
        queue = self.add_queue(event, capacity="3")
        player, _ = self.register(event, queue, "a@example.org")
        with self.app.app_context():
            reg_id = get_db().execute("SELECT id FROM registrations").fetchone()[0]
        path = f"/admin/{event['slug']}/registrations/{reg_id}/paid"
        # Only the organizer can mark payments
        self.assertEqual(self.post(player, path).status_code, 403)

        self.post(self.organizer, path)
        self.assertIn("Greiðsla staðfest", player.get(self.url(f"/me/{reg_id}")).get_data(as_text=True))
        csv_text = self.organizer.get(self.url(f"/admin/{event['slug']}/export.csv")).get_data(as_text=True)
        self.assertIn(";Greitt;", csv_text)
        self.assertIn(";já;", csv_text)

        # Clicking again removes the mark (mistake or refund)
        self.post(self.organizer, path)
        self.assertNotIn("Greiðsla staðfest", player.get(self.url(f"/me/{reg_id}")).get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT paid_at FROM registrations").fetchone()[0])

    def test_migration_from_version_1(self):
        import sqlite3
        from db import SCHEMA, init_db

        # Build a version-1 database: today's schema without the runner columns
        v1 = SCHEMA.read_text().replace("PRAGMA user_version = 3;", "PRAGMA user_version = 1;")
        v1 = re.sub(r",\s*paid_at[^\n]*\n", "\n", v1)
        v1 = re.sub(r",\s*--[^\n]*\n(\s*runner_\w+\s+TEXT,?\n)+", "\n", v1)
        v1 = re.sub(r"CREATE UNIQUE INDEX IF NOT EXISTS queues_runner_code[^;]*;", "", v1)
        self.assertNotIn("runner_", v1)
        self.assertNotIn("paid_at", v1)
        path = f"{self.tmp.name}/v1.db"
        con = sqlite3.connect(path)
        con.executescript(v1)
        con.execute("""INSERT INTO events (slug, name, starts_at, ends_at, organizer_name, organizer_email,
                       admin_code_hash, created_at) VALUES ('s', 'E', 'a', 'b', 'O', 'o@x.is', 'h', 'c')""")
        con.execute("INSERT INTO queues (event_id, name, starts_at, ends_at) VALUES (1, 'Gamalt spil', 'a', 'b')")
        con.commit()
        con.close()

        init_db(path)
        init_db(path)  # running again is harmless
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertIn("paid_at", {r[1] for r in con.execute("PRAGMA table_info(registrations)")})
        cols = {r[1] for r in con.execute("PRAGMA table_info(queues)")}
        self.assertTrue({"runner_name", "runner_email", "runner_code_hash", "runner_reminder_sent_at"} <= cols)
        self.assertEqual(con.execute("SELECT name FROM queues").fetchone()[0], "Gamalt spil")
        con.close()

    def test_cleanup_removes_unverified(self):
        import tasks

        event = self.create_event()
        queue = self.add_queue(event)
        self.register(event, queue, "slow@example.org", verify=False)
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE registrations SET created_at = ?", (to_db(utcnow() - timedelta(days=2)),))
            tasks.cleanup()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 0)


class MarkdownTest(unittest.TestCase):
    def test_renders_markdown_and_strips_html(self):
        html = str(render_markdown("**Feitt** og [hlekkur](https://example.com)\nNý lína\n\n- a\n- b\n\n"
                                   "<script>alert(1)</script> [x](javascript:alert(1))"))
        self.assertIn("<strong>Feitt</strong>", html)
        self.assertIn('href="https://example.com"', html)
        self.assertIn("<br", html)
        self.assertIn("<li>a</li>", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)


class PrefixedFlowTest(FlowTest):
    """Same flows when mounted under a sub-folder, e.g. server.com/skraning."""

    prefix = "/skraning"

    def test_links_include_prefix(self):
        event = self.create_event()
        html = self.organizer.get(self.url(f"/admin/{event['slug']}")).get_data(as_text=True)
        self.assertIn(f'href="/skraning/e/{event["slug"]}"', html)
        self.assertIn("https://example.org/skraning/e/", html)
        self.assertIn("/skraning/static/favicon.svg", html)
        self.assertIn("https://example.org/skraning/c/", mail.outbox[0].get_content())


if __name__ == "__main__":
    unittest.main()
