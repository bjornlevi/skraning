# Skráning

Registration for events with one or more games (called *queues* in the code).
Each game has its own start/end time inside the event and a capacity; later
registrations go on a waitlist and are promoted automatically when a spot opens
up. Nobody can be registered in two games that overlap in time. No accounts:
organizers and participants get a code by email, and using it verifies the email
address. Reminders are sent 24 hours before the start.

Flask + SQLite, Icelandic UI.

## Development

```bash
make install   # .venv + dependencies
make dev       # http://localhost:5003 — emails are printed to the console
make test
make tasks     # run reminders/cleanup once
```

To try the sub-folder setup locally: `PREFIX=/skraning make dev`, then open
http://localhost:5003/skraning/

## How it works

- **Codes** (`XXXX-XXXX-XXXX`) are only stored hashed. The first email contains
  the code; opening it (`/c/<code>`) verifies the email address, publishes an
  event or activates a registration, and remembers access in the session cookie.
- **Later emails** (promotion, reminder) contain signed links (`/l/...`),
  an HMAC of the stored code hash. "Týndur kóði" (`/lost`) issues new codes and
  invalidates old codes and links.
- **Waitlist position is computed**, not stored: active registrations in a
  queue ordered by verification time; the first `capacity` have a spot.
  `last_notified_state` is used to email people whose state changes
  (cancellation, capacity change, removal by organizer).
- **Time slots**: a game's `[starts_at, ends_at)` must lie within the event.
  An email may not hold two non-cancelled registrations (pending, confirmed or
  waitlisted) in overlapping games; back-to-back is fine. The check and insert
  run under `BEGIN IMMEDIATE`. Games with identical times are shown grouped, and
  the game form offers existing times as one-click choices. Registration for a
  game closes when it starts. `max_per_person` (optional) caps games per person.
- **Reminders**: one email per participant per event, 24h before the event
  starts, listing all their games.
- **Unverified** registrations hold no spot and are deleted after 24 hours;
  unverified events after 48 hours (`tasks.py`).

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `PREFIX` | *(empty)* | URL prefix behind a reverse proxy, e.g. `/skraning` |
| `BASE_URL` | `http://localhost:5003` + `PREFIX` | Public URL **including** the prefix; used for links in emails |
| `SECRET_KEY` | dev value | Required when `MAIL_BACKEND=smtp` |
| `DATABASE` | `data/skraning.db` | |
| `UPLOAD_DIR` | `data/uploads` | |
| `TIMEZONE` | `Atlantic/Reykjavik` | |
| `CREATE_PASSWORD` | *(empty)* | Shared password needed to create an event. Required when `MAIL_BACKEND=smtp`; optional in development |
| `SITE_NAME` | `Skráning` | |
| `MAIL_BACKEND` | `console` | `smtp`, `console` or `memory` |
| `MAIL_FROM` / `MAIL_FROM_NAME` | `no-reply@bjornlevi.is` / `Skráning` | |
| `SMTP_HOST` `SMTP_PORT` `SMTP_STARTTLS` `SMTP_USER` `SMTP_PASSWORD` | `localhost` `25` | `SMTP_STARTTLS=1` to enable |

## Deployment (server.com/skraning)

Runs like opin_gogn: gunicorn on localhost behind nginx, mounted at
`/skraning` via `PREFIX`. Example files are in `deploy/` — adjust the user, paths
and port to match how opin_gogn is set up on the server:

1. Copy the project to `/srv/skraning`, run `make install`, create `data/`
   owned by `www-data`.
2. `deploy/skraning.env.example` → `/etc/default/skraning` (chmod 600), fill in.
3. `deploy/skraning.service` → systemd; `deploy/nginx.conf` → the existing
   server block.
4. `deploy/skraning-tasks.service` and `deploy/skraning-tasks.timer` → systemd, then
   `sudo systemctl enable --now skraning-tasks.timer` (reminders + cleanup every 10 min,
   same user and settings file as the app; output in `journalctl -u skraning-tasks`).

Back up `data/` (the SQLite database and uploaded images).

### Email for no-reply@bjornlevi.is

Emails must not end up in spam, so the DNS for bjornlevi.is needs:

- **SPF**: TXT `v=spf1 a mx ip4:<server IP> ~all` (merge with any existing SPF record; only one is allowed)
- **DKIM**: sign outgoing mail (e.g. Postfix + OpenDKIM) and publish the key as a TXT record
- **DMARC**: TXT on `_dmarc.bjornlevi.is`: `v=DMARC1; p=none; rua=mailto:<you>`
- **Reverse DNS (PTR)** for the server IP pointing to its hostname (set at the hosting provider)

Many VPS providers block outbound port 25 by default. If so — or if
bjornlevi.is already has mail hosting — it is simpler to send through that
host with `SMTP_PORT=587`, `SMTP_STARTTLS=1` and the account's credentials;
SPF/DKIM are then handled by the mail host.
