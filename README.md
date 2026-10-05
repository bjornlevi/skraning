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

## Deployment (bjornlevi.is/skraning)

Runs like opin_gogn: gunicorn on `127.0.0.1:8018` behind nginx, mounted at
`/skraning`. The app runs as the system user `web`, and all settings live in
`/etc/default/skraning`.

### 1. Code and data folder

```bash
sudo mkdir /srv/skraning && sudo chown $USER: /srv/skraning
git clone git@github.com:bjornlevi/skraning.git /srv/skraning   # as yourself, with your GitHub key
cd /srv/skraning && make install
mkdir -p data && sudo chown -R web: data
```

### 2. Settings

```bash
sudo cp deploy/skraning.env.example /etc/default/skraning
sudo chmod 600 /etc/default/skraning
sudo nano /etc/default/skraning
```

Set at least `BASE_URL`, `SECRET_KEY` (`python3 -c "import secrets; print(secrets.token_hex(32))"`)
and `CREATE_PASSWORD`. The app refuses to start without the last two when
`MAIL_BACKEND=smtp`.

### 3. The web app (systemd + nginx)

```bash
sudo cp deploy/skraning.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skraning
curl -sI http://127.0.0.1:8018/skraning/ | head -1     # expect: HTTP/1.1 200 OK
```

Then add the `location /skraning/` block from `deploy/nginx.conf` to the
existing `server { }` block for bjornlevi.is, and reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Reminders and cleanup (systemd timer)

Every 10 minutes, `tasks.py` sends reminder emails (once per participant per
event, 24 hours before it starts) and deletes unconfirmed registrations and
events. A systemd timer runs it with the same user and settings file as the app.

```bash
sudo cp deploy/skraning-tasks.service deploy/skraning-tasks.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skraning-tasks.timer
```

Check that it works:

```bash
sudo systemctl start skraning-tasks.service              # run it once now
systemctl status skraning-tasks.service --no-pager       # expect: status=0/SUCCESS
systemctl list-timers skraning-tasks.timer               # when it runs next
journalctl -u skraning-tasks -n 20 --no-pager            # its output and any errors
```

Both service files run as `web`. The timer's service must use the same user as
the app, or it cannot write to the database. If you change the user, change
`User=` in both `skraning.service` and `skraning-tasks.service` before copying
them.

### Updating

```bash
cd /srv/skraning && git pull
sudo systemctl restart skraning
```

### Backups

Back up `/srv/skraning/data/`: the SQLite database and uploaded images.

## Email (no-reply@bjornlevi.is)

The app hands mail to a send-only Postfix on the same server
(`SMTP_HOST=localhost`, `SMTP_PORT=25`), which signs it with OpenDKIM and
delivers it. DNS for bjornlevi.is is managed in the 1984 control panel.

### DNS records

| Name | Type | Value |
|---|---|---|
| `bjornlevi.is` | TXT | `v=spf1 ip4:93.95.230.205 -all` (SPF: only this server may send) |
| `skraning._domainkey` | TXT | the DKIM public key, `v=DKIM1; h=sha256; k=rsa; p=…` (see below) |
| `_dmarc` | TXT | `v=DMARC1; p=none` |

DNSSEC is deliberately not enabled; email does not need it.

### Postfix (send-only)

```bash
sudo apt install postfix        # "Internet Site", mail name: bjornlevi.is
sudo postconf -e 'myhostname = bjornlevi.is' 'myorigin = $myhostname' \
  'inet_interfaces = loopback-only' 'inet_protocols = ipv4' \
  'mydestination = localhost' 'smtp_tls_security_level = may'
```

`loopback-only` means only programs on the server can send through it.
`ipv4` matters because SPF only lists the IPv4 address. If `postconf` warns
about duplicate entries in `/etc/postfix/main.cf`, remove the duplicates.

### OpenDKIM (signing, selector `skraning`)

```bash
sudo apt install opendkim opendkim-tools
sudo mkdir -p /etc/opendkim/keys/bjornlevi.is
sudo opendkim-genkey -b 2048 -d bjornlevi.is -s skraning -D /etc/opendkim/keys/bjornlevi.is
sudo chown -R opendkim:opendkim /etc/opendkim
```

In `/etc/opendkim.conf`, comment out existing `Socket` lines and add:

```
Domain            bjornlevi.is
Selector          skraning
KeyFile           /etc/opendkim/keys/bjornlevi.is/skraning.private
Socket            inet:8891@localhost
Canonicalization  relaxed/simple
Mode              s
```

Set `SOCKET=inet:8891@localhost` in `/etc/default/opendkim`, then connect Postfix:

```bash
sudo systemctl daemon-reload && sudo systemctl restart opendkim
sudo postconf -e 'milter_default_action = accept' 'milter_protocol = 6' \
  'smtpd_milters = inet:localhost:8891' 'non_smtpd_milters = inet:localhost:8891'
sudo systemctl restart postfix
```

The value for the `skraning._domainkey` DNS record, as one line:

```bash
sudo sed -n 's/.*"\(.*\)".*/\1/p' /etc/opendkim/keys/bjornlevi.is/skraning.txt | tr -d '\n'; echo
```

If the server is rebuilt with a new key, this DNS record must be updated too.

### Testing

```bash
sudo opendkim-testkey -d bjornlevi.is -s skraning -vvv    # "key OK" ("key not secure" = no DNSSEC, fine)
printf 'From: no-reply@bjornlevi.is\nTo: you@gmail.com\nSubject: Prufa\n\nHallo\n' \
  | sendmail -f no-reply@bjornlevi.is you@gmail.com
sudo tail -n 20 /var/log/mail.log                          # look for status=sent
```

In Gmail, "Show original" should report SPF, DKIM and DMARC as PASS.
Messages from a new sender may still land in spam at first; marking them
"not spam" helps, and the app tells participants to check their spam folder.
https://www.mail-tester.com gives a detailed score.
