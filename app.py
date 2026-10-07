"""Skráning — event registration with queues and waitlists, no user accounts.

Organizers and participants identify themselves with a code that is emailed to
them; using the code (or the link containing it) also verifies the email
address. Later emails carry signed links instead, since only code hashes are
stored.
"""

import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
import markdown as markdown_lib
import nh3
from markupsafe import Markup, escape
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix

import mail
from db import (
    close_db,
    from_db,
    get_db,
    hash_code,
    init_db,
    new_code,
    normalize_code,
    now_db,
    overlapping_registration,
    queue_counts,
    queue_standing,
    rate_exceeded,
    rate_hit,
    rate_limited,
    registration_state,
    sync_queue_states,
    to_db,
    utcnow,
)

BASE_DIR = Path(__file__).parent

# URL prefix for reverse proxy deployment, e.g. "/skraning"
PREFIX = os.getenv("PREFIX", "").rstrip("/")

log = logging.getLogger(__name__)


def load_config() -> dict:
    base_url = os.getenv("BASE_URL", f"http://localhost:5003{PREFIX}").rstrip("/")
    return {
        "PREFIX": PREFIX,
        "SECRET_KEY": os.getenv("SECRET_KEY", "dev-only-not-secret"),
        "DATABASE": os.getenv("DATABASE", str(BASE_DIR / "data" / "skraning.db")),
        "UPLOAD_DIR": os.getenv("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")),
        # Public address including PREFIX — used for links in emails (incl. from tasks.py)
        "BASE_URL": base_url,
        "TIMEZONE": os.getenv("TIMEZONE", "Atlantic/Reykjavik"),
        # Shared association password for creating events (required in production)
        "CREATE_PASSWORD": os.getenv("CREATE_PASSWORD", ""),
        "SITE_NAME": os.getenv("SITE_NAME", "Skráning"),
        "MAIL_BACKEND": os.getenv("MAIL_BACKEND", "console"),
        "MAIL_FROM": os.getenv("MAIL_FROM", "no-reply@bjornlevi.is"),
        "MAIL_FROM_NAME": os.getenv("MAIL_FROM_NAME", "Skráning"),
        "SMTP_HOST": os.getenv("SMTP_HOST", "localhost"),
        "SMTP_PORT": int(os.getenv("SMTP_PORT", "25")),
        "SMTP_STARTTLS": os.getenv("SMTP_STARTTLS", "") == "1",
        "SMTP_USER": os.getenv("SMTP_USER", ""),
        "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
        "MAX_CONTENT_LENGTH": 10 * 1024 * 1024,
        "PERMANENT_SESSION_LIFETIME": timedelta(days=180),
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": base_url.startswith("https://"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DAYS = ["mánudagur", "þriðjudagur", "miðvikudagur", "fimmtudagur", "föstudagur", "laugardagur", "sunnudagur"]
# Accusative, for dates after a verb: "Skráning opnar þriðjudaginn 6. október"
DAYS_ACC = ["mánudaginn", "þriðjudaginn", "miðvikudaginn", "fimmtudaginn", "föstudaginn", "laugardaginn",
            "sunnudaginn"]
MONTHS = ["janúar", "febrúar", "mars", "apríl", "maí", "júní",
          "júlí", "ágúst", "september", "október", "nóvember", "desember"]

STATE_LABELS = {
    "confirmed": "Staðfest pláss",
    "waitlisted": "Á biðlista",
    "pending": "Óstaðfest",
    "cancelled": "Afskráð",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_TRANSLIT = str.maketrans({"þ": "th", "Þ": "th", "æ": "ae", "Æ": "ae", "ð": "d", "Ð": "d", "ö": "o", "Ö": "o"})


def local_tz() -> ZoneInfo:
    return ZoneInfo(current_app.config["TIMEZONE"])


def to_local(value: str | None) -> datetime | None:
    dt = from_db(value)
    return dt.astimezone(local_tz()) if dt else None


def fmt_dt(value: str | None, with_day: bool = True, accusative: bool = False) -> str:
    d = to_local(value)
    if d is None:
        return ""
    day = f"{(DAYS_ACC if accusative else DAYS)[d.weekday()]} " if with_day else ""
    return f"{day}{d.day}. {MONTHS[d.month - 1]} {d.year} kl. {d:%H:%M}"


def fmt_range(start: str | None, end: str | None) -> str:
    """'laugardagur 14. nóvember 2026 kl. 13:00–17:00', or both ends in full across days."""
    s, e = to_local(start), to_local(end)
    if s is None or e is None:
        return fmt_dt(start)
    if s.date() == e.date():
        return f"{fmt_dt(start)}–{e:%H:%M}"
    return f"{fmt_dt(start)} – {fmt_dt(end)}"


TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.]?(\d{2}))?\s*$")


def fmt_slot(start: str | None, end: str | None, event) -> str:
    """Short slot heading: just the times when the event is a single day."""
    s, e = to_local(start), to_local(end)
    if s is None or e is None:
        return ""
    times = f"kl. {s:%H:%M}–{e:%H:%M}"
    if to_local(event["starts_at"]).date() == to_local(event["ends_at"]).date():
        return times
    return f"{DAYS[s.weekday()].capitalize()} {s.day}. {MONTHS[s.month - 1]} {times}"


def parse_local(date_text: str, time_text: str) -> datetime | None:
    """Date from a date input + 24-hour time typed as '13:00', '13.00', '1300' or '13'."""
    m = TIME_RE.match(time_text or "")
    if not m:
        return None
    try:
        day = datetime.strptime((date_text or "").strip(), "%Y-%m-%d")
        return day.replace(hour=int(m[1]), minute=int(m[2] or 0), tzinfo=local_tz())
    except ValueError:  # bad date, hour > 23 or minute > 59
        return None


def parse_optional_local(form, prefix: str, errors: dict) -> datetime | None:
    """Optional date + time pair named <prefix>_date / <prefix>_time."""
    date_text, time_text = form.get(f"{prefix}_date", ""), form.get(f"{prefix}_time", "")
    if not date_text.strip() and not time_text.strip():
        return None
    dt = parse_local(date_text, time_text)
    if dt is None:
        errors[prefix] = "Sláðu inn bæði dagsetningu og tíma (t.d. 13:00)."
    return dt


def local_input(value: str | None, fmt: str) -> str:
    d = to_local(value)
    return d.strftime(fmt) if d else ""


def nl2br(text: str | None) -> Markup:
    return Markup("<br>\n").join(escape(text or "").split("\n"))


MD_TAGS = {"p", "br", "strong", "em", "a", "ul", "ol", "li", "h1", "h2", "h3", "h4",
           "blockquote", "code", "pre", "hr", "del"}


def render_markdown(text: str | None) -> Markup:
    """Markdown for descriptions; single newlines become line breaks, any HTML is stripped."""
    html = markdown_lib.markdown(text or "", extensions=["nl2br", "sane_lists"])
    return Markup(nh3.clean(html, tags=MD_TAGS, attributes={"a": {"href"}},
                            url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow"))


def make_slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.translate(SLUG_TRANSLIT))
    s = re.sub(r"[^a-z0-9]+", "-", s.encode("ascii", "ignore").decode().lower())
    s = s.strip("-")[:40].strip("-") or "vidburdur"
    return f"{s}-{secrets.token_hex(2)}"


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(EMAIL_RE.match(email))


def client_ip() -> str:
    return request.remote_addr or "unknown"


def external_url(endpoint: str, **values) -> str:
    """Absolute URL for emails. Works outside requests (tasks.py) via BASE_URL."""
    path = current_app.url_map.bind("localhost").build(endpoint, values)
    return current_app.config["BASE_URL"] + path


def link_sig(kind: str, obj_id: int, code_hash: str) -> str:
    """Signature for emailed links; invalidated when the code is reset."""
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{kind}:{obj_id}:{code_hash}".encode(), hashlib.sha256).hexdigest()[:16]


def registration_link(reg) -> str:
    return external_url("signed_access", kind="r", obj_id=reg["id"],
                        sig=link_sig("r", reg["id"], reg["access_code_hash"]))


def runner_link(queue) -> str:
    return external_url("signed_access", kind="g", obj_id=queue["id"],
                        sig=link_sig("g", queue["id"], queue["runner_code_hash"]))


def runner_grant_key(queue) -> str:
    """Session key for a game runner. Includes part of the code hash, so access ends
    when the organizer changes or removes the runner (which replaces the code)."""
    return f"{queue['id']}:{(queue['runner_code_hash'] or '')[:16]}"


def event_admin_link(event) -> str:
    return external_url("signed_access", kind="e", obj_id=event["id"],
                        sig=link_sig("e", event["id"], event["admin_code_hash"]))


def save_image(file) -> str | None:
    """Resize, re-encode as JPEG (drops EXIF) and store. Returns the file name."""
    if not file or not file.filename:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(file.stream))
        img.thumbnail((1600, 1600))
        if img.mode != "RGB":
            rgba = img.convert("RGBA")
            img = Image.new("RGB", rgba.size, "white")
            img.paste(rgba, mask=rgba.split()[-1])
        name = f"{uuid.uuid4().hex}.jpg"
        upload_dir = Path(current_app.config["UPLOAD_DIR"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        img.save(upload_dir / name, "JPEG", quality=85, optimize=True)
        return name
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("Gat ekki lesið myndina. Notaðu JPG, PNG eða WebP.")


def delete_image(name: str | None) -> None:
    if name:
        (Path(current_app.config["UPLOAD_DIR"]) / name).unlink(missing_ok=True)


def replace_image(current: str | None, new_image: str | None) -> str | None:
    """Keep, replace or remove a stored image according to the submitted form."""
    if new_image or request.form.get("remove_image"):
        delete_image(current)
        return new_image
    return current


def grant(kind: str, obj_id: int) -> None:
    """Remember in the session that this browser has used the code for an object."""
    ids = [i for i in session.get(kind, []) if i != obj_id]
    session[kind] = (ids + [obj_id])[-200:]
    session.permanent = True


def has_access(kind: str, obj_id: int) -> bool:
    return obj_id in session.get(kind, [])


def registration_closed_reason(event) -> str | None:
    now = now_db()
    if event["status"] == "cancelled":
        return "Viðburðinum hefur verið aflýst."
    if now >= event["ends_at"]:
        return "Viðburðinum er lokið."
    if event["registration_opens_at"] and now < event["registration_opens_at"]:
        return f"Skráning opnar {fmt_dt(event['registration_opens_at'], accusative=True)}."
    if event["registration_closes_at"] and now >= event["registration_closes_at"]:
        return "Skráningu er lokið."
    return None


def queue_closed_reason(queue) -> str | None:
    """Why a single game is closed (on top of registration_closed_reason for the event)."""
    if not queue["is_open"]:
        return "Lokað fyrir skráningu."
    if now_db() >= queue["starts_at"]:
        return "Hafið."
    return None


def registration_conflict(db, event, queue, email: str) -> str | None:
    """Why `email` may not register for `queue`, or None."""
    if db.execute(
        "SELECT 1 FROM registrations WHERE queue_id = ? AND email = ? AND cancelled_at IS NULL",
        (queue["id"], email),
    ).fetchone():
        return ("Þetta netfang er þegar skráð í þetta spil. "
                "Notaðu „Týndur kóði“ ef þú finnur ekki tölvupóstinn.")
    clash = overlapping_registration(db, event["id"], email, queue["starts_at"], queue["ends_at"])
    if clash:
        return (f"Þú ert þegar skráð/ur í „{clash['queue_name']}“ "
                f"({fmt_range(clash['queue_starts_at'], clash['queue_ends_at'])}), sem skarast á "
                "við þennan tíma. Afskráðu þig þar fyrst ef þú vilt skipta.")
    if event["max_per_person"] is not None:
        (count,) = db.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND email = ? AND cancelled_at IS NULL",
            (event["id"], email),
        ).fetchone()
        if count >= event["max_per_person"]:
            return f"Hver þátttakandi má skrá sig í mest {event['max_per_person']} spil á þessum viðburði."
    return None


def notify_state_changes(changed: list[dict], event, queue) -> None:
    for reg in changed:
        if reg["state"] == "confirmed":
            subject = f"Þú fékkst pláss: {queue['name']} – {event['name']}"
        else:
            subject = f"Breyting á skráningu: {queue['name']} – {event['name']}"
        mail.send(reg["email"], subject, "state_changed.txt",
                  reg=reg, event=event, queue=queue, link=registration_link(reg))


def invite_runner(event, queue) -> bool:
    """Give the game's runner a new code (replacing any old one) and email it."""
    db = get_db()
    code = new_code()
    with db:
        db.execute("UPDATE queues SET runner_code_hash = ?, runner_reminder_sent_at = NULL WHERE id = ?",
                   (hash_code(code), queue["id"]))
    return mail.send(queue["runner_email"], f"Þú stjórnar „{queue['name']}“ – {event['name']}",
                     "runner_invite.txt", event=event, queue=queue, code=code,
                     link=external_url("access", code=code))


def cancel_registration(reg, event, queue) -> None:
    db = get_db()
    with db:
        db.execute("UPDATE registrations SET cancelled_at = ? WHERE id = ?", (now_db(), reg["id"]))
    notify_state_changes(sync_queue_states(db, queue["id"]), event, queue)


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


def parse_event_form(form, creating: bool) -> tuple[dict, dict]:
    errors: dict[str, str] = {}
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    location = form.get("location", "").strip()

    if not name:
        errors["name"] = "Nafn vantar."
    elif len(name) > 150:
        errors["name"] = "Nafnið er of langt."
    if len(description) > 10000:
        errors["description"] = "Lýsingin er of löng."
    if len(location) > 300:
        errors["location"] = "Staðsetningin er of löng."

    starts = parse_local(form.get("date", ""), form.get("time", ""))
    ends = parse_local(form.get("end_date") or form.get("date", ""), form.get("end_time", ""))
    if starts is None:
        errors["date"] = "Dagsetningu eða tíma vantar (tími á forminu 13:00)."
    elif creating and starts <= utcnow():
        errors["date"] = "Viðburðurinn þarf að vera í framtíðinni."
    if ends is None:
        errors["end"] = "Lokatíma vantar (á forminu 23:00)."
    elif starts and ends <= starts:
        errors["end"] = "Viðburðinum þarf að ljúka eftir að hann hefst."

    opens = parse_optional_local(form, "registration_opens", errors)
    closes = parse_optional_local(form, "registration_closes", errors)
    if opens and closes and closes <= opens:
        errors["registration_closes"] = "Skráningu þarf að ljúka eftir að hún opnar."

    max_per_person = None
    if form.get("max_per_person", "").strip():
        try:
            max_per_person = int(form["max_per_person"])
            if not 1 <= max_per_person <= 50:
                raise ValueError
        except ValueError:
            errors["max_per_person"] = "Sláðu inn tölu á bilinu 1–50 eða hafðu autt."
            max_per_person = None

    values = {
        "name": name,
        "description": description,
        "location": location,
        "starts_at": to_db(starts),
        "ends_at": to_db(ends),
        "registration_opens_at": to_db(opens),
        "registration_closes_at": to_db(closes),
        "max_per_person": max_per_person,
        "listed": 1 if form.get("listed") else 0,
    }

    if creating:
        organizer_name = form.get("organizer_name", "").strip()
        organizer_email = form.get("organizer_email", "").strip().lower()
        if not organizer_name or len(organizer_name) > 100:
            errors["organizer_name"] = "Nafn vantar."
        if not valid_email(organizer_email):
            errors["organizer_email"] = "Ógilt netfang."
        password = current_app.config["CREATE_PASSWORD"]
        if password and not secrets.compare_digest(form.get("create_password", ""), password):
            errors["create_password"] = "Rangt lykilorð."
        values.update(organizer_name=organizer_name, organizer_email=organizer_email)

    return values, errors


def parse_game_details(form, errors: dict) -> dict:
    """Name, description and capacity: the fields both organizers and game runners edit."""
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    if not name:
        errors["name"] = "Nafn vantar."
    elif len(name) > 150:
        errors["name"] = "Nafnið er of langt."
    if len(description) > 10000:
        errors["description"] = "Lýsingin er of löng."

    capacity = None
    if form.get("capacity", "").strip():
        try:
            capacity = int(form["capacity"])
            if not 0 <= capacity <= 100000:
                raise ValueError
        except ValueError:
            errors["capacity"] = "Sláðu inn jákvæða heiltölu eða hafðu autt."
    return {"name": name, "description": description, "capacity": capacity}


def parse_queue_form(form, event) -> tuple[dict, dict]:
    errors: dict[str, str] = {}
    details = parse_game_details(form, errors)

    runner_name = form.get("runner_name", "").strip()
    runner_email = form.get("runner_email", "").strip().lower()
    if len(runner_name) > 100:
        errors["runner_name"] = "Nafnið er of langt."
    if runner_email and not valid_email(runner_email):
        errors["runner_email"] = "Ógilt netfang."

    starts = parse_local(form.get("date", ""), form.get("start_time", ""))
    ends = parse_local(form.get("date", ""), form.get("end_time", ""))
    if starts is None or ends is None:
        errors["time"] = "Dagsetningu, upphafs- eða lokatíma vantar (tími á forminu 13:00)."
    elif ends == starts:
        errors["time"] = "Spilinu þarf að ljúka eftir að það hefst."
    else:
        if ends < starts:  # e.g. 22:00–02:00 ends the next day
            ends += timedelta(days=1)
    if not errors.get("time") and (to_db(starts) < event["starts_at"] or to_db(ends) > event["ends_at"]):
        errors["time"] = f"Tíminn þarf að vera innan viðburðarins: {fmt_range(event['starts_at'], event['ends_at'])}."

    try:
        sort_order = int(form.get("sort_order") or 0)
    except ValueError:
        sort_order = 0

    return {
        **details,
        "starts_at": to_db(starts),
        "ends_at": to_db(ends),
        "sort_order": sort_order,
        "is_open": 1 if form.get("is_open") else 0,
        "runner_name": runner_name or None,
        "runner_email": runner_email or None,
    }, errors


def time_slots(event) -> list[dict]:
    """Distinct game times in an event, for quick picking in the game form."""
    return [
        {"date": local_input(r["starts_at"], "%Y-%m-%d"),
         "start_time": local_input(r["starts_at"], "%H:%M"),
         "end_time": local_input(r["ends_at"], "%H:%M"),
         "label": fmt_range(r["starts_at"], r["ends_at"]), "count": r["n"]}
        for r in get_db().execute(
            """SELECT starts_at, ends_at, COUNT(*) AS n FROM queues WHERE event_id = ?
               GROUP BY starts_at, ends_at ORDER BY starts_at, ends_at""",
            (event["id"],),
        )
    ]


def event_form_values(event) -> dict:
    """Values to prefill the event form from a stored event."""
    return {
        **dict(event),
        "date": local_input(event["starts_at"], "%Y-%m-%d"),
        "time": local_input(event["starts_at"], "%H:%M"),
        "end_date": local_input(event["ends_at"], "%Y-%m-%d"),
        "end_time": local_input(event["ends_at"], "%H:%M"),
        "max_per_person": "" if event["max_per_person"] is None else event["max_per_person"],
        "registration_opens_date": local_input(event["registration_opens_at"], "%Y-%m-%d"),
        "registration_opens_time": local_input(event["registration_opens_at"], "%H:%M"),
        "registration_closes_date": local_input(event["registration_closes_at"], "%Y-%m-%d"),
        "registration_closes_time": local_input(event["registration_closes_at"], "%H:%M"),
        "listed": "1" if event["listed"] else "",
    }


def queue_form_values(queue) -> dict:
    return {
        **dict(queue),
        "capacity": "" if queue["capacity"] is None else queue["capacity"],
        "date": local_input(queue["starts_at"], "%Y-%m-%d"),
        "start_time": local_input(queue["starts_at"], "%H:%M"),
        "end_time": local_input(queue["ends_at"], "%H:%M"),
        "is_open": "1" if queue["is_open"] else "",
        "runner_name": queue["runner_name"] or "",
        "runner_email": queue["runner_email"] or "",
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(overrides: dict | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)

    if app.config["MAIL_BACKEND"] == "smtp":
        # Production: refuse to start without a real secret and the association's password
        if app.config["SECRET_KEY"] == "dev-only-not-secret":
            raise RuntimeError("Set SECRET_KEY before sending real email.")
        if not app.config["CREATE_PASSWORD"]:
            raise RuntimeError("Set CREATE_PASSWORD (the password needed to create events).")

    prefix = app.config["PREFIX"]
    if prefix:
        app.config["APPLICATION_ROOT"] = prefix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

        class PrefixMiddleware:
            def __init__(self, app, prefix: str):
                self.app = app
                self.prefix = prefix

            def __call__(self, environ, start_response):
                script_name = self.prefix
                path_info = environ.get("PATH_INFO", "")
                if path_info.startswith(script_name):
                    environ["SCRIPT_NAME"] = script_name
                    environ["PATH_INFO"] = path_info[len(script_name):] or "/"
                return self.app(environ, start_response)

        app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix)

    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    app.jinja_env.filters.update(dt=fmt_dt, dt_acc=lambda v: fmt_dt(v, accusative=True), dtrange=fmt_range, slot_label=fmt_slot, nl2br=nl2br, md=render_markdown)
    app.jinja_env.globals.update(STATE_LABELS=STATE_LABELS)

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    # Globals (not a context processor) so imported macros can use them too.
    app.jinja_env.globals.update(
        csrf_field=lambda: Markup(f'<input type="hidden" name="csrf" value="{csrf_token()}">'),
        site_name=app.config["SITE_NAME"],
    )

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf", "")
            if not expected or not secrets.compare_digest(request.form.get("csrf", ""), expected):
                abort(400, "Eyðublaðið er útrunnið. Farðu til baka, endurhlaðaðu síðuna og reyndu aftur.")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def error_page(err):
        messages = {
            404: "Síðan fannst ekki.",
            413: "Skráin er of stór (hámark 10 MB).",
            429: "Of margar tilraunir. Reyndu aftur eftir smá stund.",
        }
        message = err.description if err.code == 400 else messages[err.code]
        return render_template("message.html", title="Villa", message=message), err.code

    @app.errorhandler(403)
    def forbidden(_err):
        return render_template(
            "message.html",
            title="Aðgangur",
            message="Sláðu inn kóðann sem þú fékkst í tölvupósti til að opna þessa síðu.",
            show_lookup=True,
        ), 403

    # ===========================================================================
    # PUBLIC
    # ===========================================================================

    @app.route("/")
    def home():
        db = get_db()
        events = db.execute(
            """SELECT * FROM events WHERE status = 'published' AND listed = 1 AND ends_at > ?
               ORDER BY starts_at""",
            (now_db(),),
        ).fetchall()
        return render_template("home.html", events=events)

    @app.route("/uploads/<name>")
    def upload(name):
        return send_from_directory(app.config["UPLOAD_DIR"], name, max_age=7 * 24 * 3600)

    @app.route("/sent")
    def sent():
        return render_template("sent.html")

    @app.route("/e/<slug>")
    def event_page(slug):
        return render_event_page(slug)

    @app.route("/e/<slug>/<int:queue_id>")
    def game_page(slug, queue_id):
        """Shareable link to one game: the event page with that game picked and in view."""
        return render_event_page(slug, focus=queue_id)

    def render_event_page(slug, form=None, errors=None, status=200, focus=None):
        db = get_db()
        event = db.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
        if event is None or event["status"] == "pending":
            abort(404)
        mine = [
            {"reg": r, **registration_state(db, r)}
            for r in db.execute(
                """SELECT r.*, q.name AS queue_name, q.starts_at AS queue_starts_at,
                          q.ends_at AS queue_ends_at
                   FROM registrations r JOIN queues q ON q.id = r.queue_id
                   WHERE r.event_id = ? ORDER BY q.starts_at""",
                (event["id"],),
            )
            if has_access("regs", r["id"])
        ]
        active_mine = [m["reg"] for m in mine if m["state"] != "cancelled"]

        # Games grouped into time slots (games with identical start and end)
        slots: list[dict] = []
        for q in db.execute(
            "SELECT * FROM queues WHERE event_id = ? ORDER BY starts_at, ends_at, sort_order, id",
            (event["id"],),
        ).fetchall():
            if not slots or (slots[-1]["starts_at"], slots[-1]["ends_at"]) != (q["starts_at"], q["ends_at"]):
                slots.append({"starts_at": q["starts_at"], "ends_at": q["ends_at"], "items": []})
            slots[-1]["items"].append({
                "queue": q,
                "counts": queue_counts(db, q),
                "closed": queue_closed_reason(q),
                "registered": any(r["queue_id"] == q["id"] for r in active_mine),
                "clash": next((r for r in active_mine if r["queue_id"] != q["id"]
                               and r["queue_starts_at"] < q["ends_at"]
                               and q["starts_at"] < r["queue_ends_at"]), None),
            })
        focus_queue = None
        if focus is not None:
            focus_queue = next((i["queue"] for s in slots for i in s["items"] if i["queue"]["id"] == focus), None)
            if focus_queue is None:
                abort(404)
            if form is None:
                form = {"queue_id": str(focus)}
        return render_template(
            "event.html",
            focus_queue=focus_queue,
            event=event,
            slots=slots,
            mine=mine,
            closed_reason=registration_closed_reason(event),
            is_admin=has_access("events", event["id"]),
            form=form or {},
            errors=errors or {},
        ), status

    @app.route("/e/<slug>/register", methods=["POST"])
    def register(slug):
        db = get_db()
        event = db.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
        if event is None or event["status"] == "pending":
            abort(404)
        if request.form.get("website"):  # honeypot
            return redirect(url_for("sent"))

        errors: dict[str, str] = {}
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        queue = db.execute(
            "SELECT * FROM queues WHERE id = ? AND event_id = ?",
            (request.form.get("queue_id", type=int), event["id"]),
        ).fetchone()

        reason = registration_closed_reason(event)
        if reason:
            errors["form"] = reason
        if queue is None:
            errors["queue_id"] = "Veldu spil."
        elif queue_closed_reason(queue):
            errors["queue_id"] = f"„{queue['name']}“: {queue_closed_reason(queue)}"
        if not name or len(name) > 100:
            errors["name"] = "Nafn vantar."
        if not valid_email(email):
            errors["email"] = "Ógilt netfang."

        if not errors and (rate_limited(f"register-ip:{client_ip()}", 20)
                           or rate_limited(f"register-email:{email}", 6)):
            abort(429)

        code = new_code()
        if not errors:
            # Check and insert under a write lock so two simultaneous requests
            # cannot both slip past the overlap check.
            db.execute("BEGIN IMMEDIATE")
            try:
                conflict = registration_conflict(db, event, queue, email)
                if conflict:
                    errors["email"] = conflict
                else:
                    reg_id = db.execute(
                        """INSERT INTO registrations
                               (queue_id, event_id, name, email, access_code_hash, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (queue["id"], event["id"], name, email, hash_code(code), now_db()),
                    ).lastrowid
                db.commit()
            except BaseException:
                db.rollback()
                raise

        if errors:
            return render_event_page(slug, form=request.form, errors=errors, status=400)
        ok = mail.send(email, f"Staðfestu skráningu: {event['name']}", "registration_verify.txt",
                       name=name, event=event, queue=queue, code=code,
                       link=external_url("access", code=code))
        if not ok:
            with db:
                db.execute("DELETE FROM registrations WHERE id = ?", (reg_id,))
            errors["form"] = "Ekki tókst að senda tölvupóst. Reyndu aftur síðar."
            return render_event_page(slug, form=request.form, errors=errors, status=500)
        return redirect(url_for("sent", to="reg"))

    # ===========================================================================
    # CODES & ACCESS
    # ===========================================================================

    def open_event(event):
        db = get_db()
        if event["status"] == "pending":
            with db:
                db.execute(
                    "UPDATE events SET status = 'published', verified_at = ? WHERE id = ?",
                    (now_db(), event["id"]),
                )
            flash("Netfangið er staðfest og viðburðurinn birtur. Bættu nú við spilum.")
        grant("events", event["id"])
        return redirect(url_for("admin", slug=event["slug"]))

    def open_registration(reg):
        db = get_db()
        if reg["verified_at"] is None and reg["cancelled_at"] is None:
            with db:
                db.execute("UPDATE registrations SET verified_at = ? WHERE id = ?", (now_db(), reg["id"]))
            sync_queue_states(db, reg["queue_id"])
            flash("Netfangið er staðfest og skráningin virk.")
        grant("regs", reg["id"])
        return redirect(url_for("status", reg_id=reg["id"]))

    def open_game(queue):
        grant("games", runner_grant_key(queue))
        return redirect(url_for("runner", queue_id=queue["id"]))

    @app.route("/c/<code>")
    def access(code):
        ip_key = f"code-fail:{client_ip()}"
        if rate_exceeded(ip_key, 30):
            abort(429)
        db = get_db()
        h = hash_code(code)
        event = db.execute("SELECT * FROM events WHERE admin_code_hash = ?", (h,)).fetchone()
        if event:
            return open_event(event)
        reg = db.execute("SELECT * FROM registrations WHERE access_code_hash = ?", (h,)).fetchone()
        if reg:
            return open_registration(reg)
        queue = db.execute("SELECT * FROM queues WHERE runner_code_hash = ?", (h,)).fetchone()
        if queue:
            return open_game(queue)
        rate_hit(ip_key)
        return render_template("message.html", title="Kóði fannst ekki",
                               message="Enginn viðburður eða skráning fannst með þessum kóða.",
                               show_lookup=True), 404

    @app.route("/l/<kind>/<int:obj_id>/<sig>")
    def signed_access(kind, obj_id, sig):
        db = get_db()
        table, column = {"e": ("events", "admin_code_hash"),
                         "r": ("registrations", "access_code_hash"),
                         "g": ("queues", "runner_code_hash")}.get(kind, (None, None))
        if table is None:
            abort(404)
        row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (obj_id,)).fetchone()
        if (row is None or row[column] is None
                or not secrets.compare_digest(sig, link_sig(kind, obj_id, row[column]))):
            return render_template("message.html", title="Hlekkur útrunninn",
                                   message="Þessi hlekkur er ekki lengur gildur. Notaðu kóðann "
                                           "eða „Týndur kóði“ til að fá nýjan.",
                                   show_lookup=True), 404
        return {"e": open_event, "r": open_registration, "g": open_game}[kind](row)

    @app.route("/lookup", methods=["POST"])
    def lookup():
        code = normalize_code(request.form.get("code", ""))
        if not code:
            return redirect(url_for("home"))
        return redirect(url_for("access", code=code))

    @app.route("/lost", methods=["GET", "POST"])
    def lost():
        if request.method == "GET":
            return render_template("lost.html")
        email = request.form.get("email", "").strip().lower()
        if request.form.get("website") or not valid_email(email):
            flash("Sláðu inn gilt netfang.")
            return render_template("lost.html"), 400
        if rate_limited(f"lost-ip:{client_ip()}", 5) or rate_limited(f"lost-email:{email}", 3):
            abort(429)

        db = get_db()
        now = now_db()
        items = []
        with db:
            for event in db.execute(
                """SELECT * FROM events WHERE organizer_email = ? AND status != 'cancelled'
                   AND ends_at > ? ORDER BY starts_at""",
                (email, now),
            ).fetchall():
                code = new_code()
                db.execute("UPDATE events SET admin_code_hash = ? WHERE id = ?", (hash_code(code), event["id"]))
                items.append({"label": f"Umsjón: {event['name']}", "code": code,
                              "link": external_url("access", code=code)})
            for reg in db.execute(
                """SELECT r.*, e.name AS event_name, q.name AS queue_name
                   FROM registrations r JOIN events e ON e.id = r.event_id
                   JOIN queues q ON q.id = r.queue_id
                   WHERE r.email = ? AND r.cancelled_at IS NULL AND e.status != 'cancelled'
                   AND q.ends_at > ? ORDER BY q.starts_at""",
                (email, now),
            ).fetchall():
                code = new_code()
                db.execute("UPDATE registrations SET access_code_hash = ? WHERE id = ?",
                           (hash_code(code), reg["id"]))
                items.append({"label": f"Skráning: {reg['event_name']} – {reg['queue_name']}",
                              "code": code, "link": external_url("access", code=code)})
            for queue in db.execute(
                """SELECT q.*, e.name AS event_name FROM queues q JOIN events e ON e.id = q.event_id
                   WHERE q.runner_email = ? AND e.status != 'cancelled' AND q.ends_at > ?
                   ORDER BY q.starts_at""",
                (email, now),
            ).fetchall():
                code = new_code()
                db.execute("UPDATE queues SET runner_code_hash = ? WHERE id = ?", (hash_code(code), queue["id"]))
                items.append({"label": f"Stjórnandi: {queue['event_name']} – {queue['name']}",
                              "code": code, "link": external_url("access", code=code)})
        if items:
            mail.send(email, "Kóðarnir þínir", "lost_codes.txt", items=items)
        return redirect(url_for("sent", to="lost"))

    # ===========================================================================
    # PARTICIPANT
    # ===========================================================================

    def load_registration(reg_id):
        if not has_access("regs", reg_id):
            abort(403)
        db = get_db()
        reg = db.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
        if reg is None:
            abort(404)
        queue = db.execute("SELECT * FROM queues WHERE id = ?", (reg["queue_id"],)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id = ?", (reg["event_id"],)).fetchone()
        return reg, queue, event

    @app.route("/me/<int:reg_id>")
    def status(reg_id):
        reg, queue, event = load_registration(reg_id)
        return render_template("status.html", reg=reg, queue=queue, event=event,
                               **registration_state(get_db(), reg),
                               counts=queue_counts(get_db(), queue))

    @app.route("/me/<int:reg_id>/cancel", methods=["POST"])
    def cancel(reg_id):
        reg, queue, event = load_registration(reg_id)
        if not request.form.get("confirm"):
            flash("Hakaðu í reitinn til að staðfesta afskráningu.")
        elif reg["cancelled_at"] is None:
            cancel_registration(reg, event, queue)
            flash("Þú hefur verið afskráð/ur.")
        return redirect(url_for("status", reg_id=reg_id))

    # ===========================================================================
    # GAME RUNNER
    # ===========================================================================

    @app.route("/game/<int:queue_id>", methods=["GET", "POST"])
    def runner(queue_id):
        db = get_db()
        queue = db.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
        if queue is None or queue["runner_code_hash"] is None or not has_access("games", runner_grant_key(queue)):
            abort(403)
        event = db.execute("SELECT * FROM events WHERE id = ?", (queue["event_id"],)).fetchone()
        can_edit = event["status"] != "cancelled"

        errors: dict[str, str] = {}
        values = queue_form_values(queue)
        if request.method == "POST" and can_edit:
            # Runners edit name, description, image and capacity; time and registration
            # stay with the organizer.
            details = parse_game_details(request.form, errors)
            new_image = None
            if not errors:
                try:
                    new_image = save_image(request.files.get("image"))
                except ValueError as e:
                    errors["image"] = str(e)
            if not errors:
                with db:
                    db.execute(
                        "UPDATE queues SET name = ?, description = ?, capacity = ?, image = ? WHERE id = ?",
                        (details["name"], details["description"], details["capacity"],
                         replace_image(queue["image"], new_image), queue_id),
                    )
                updated = db.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
                changed = sync_queue_states(db, queue_id)
                notify_state_changes(changed, event, updated)
                flash("Breytingar vistaðar." + (f" {len(changed)} þátttakendum var tilkynnt um breytta stöðu."
                                                if changed else ""))
                return redirect(url_for("runner", queue_id=queue_id))
            values = {**values, **request.form}

        (pending,) = db.execute(
            "SELECT COUNT(*) FROM registrations WHERE queue_id = ? AND verified_at IS NULL AND cancelled_at IS NULL",
            (queue_id,),
        ).fetchone()
        return render_template("game.html", event=event, queue=queue, values=values, errors=errors,
                               can_edit=can_edit, standing=queue_standing(db, queue), pending=pending,
                               public_url=external_url("game_page", slug=event["slug"], queue_id=queue_id),
                               ), (400 if errors else 200)

    # ===========================================================================
    # ORGANIZER
    # ===========================================================================

    @app.route("/new", methods=["GET", "POST"])
    def new_event():
        need_password = bool(app.config["CREATE_PASSWORD"])
        if request.method == "GET":
            return render_template("event_form.html", creating=True, need_password=need_password,
                                   values={"listed": "1"}, errors={})
        if request.form.get("website"):  # honeypot
            return redirect(url_for("sent", to="event"))

        values, errors = parse_event_form(request.form, creating=True)
        if not errors:
            try:
                values["image"] = save_image(request.files.get("image"))
            except ValueError as e:
                errors["image"] = str(e)
        if errors:
            delete_image(values.get("image"))
            return render_template("event_form.html", creating=True, need_password=need_password,
                                   values=request.form, errors=errors), 400
        if (rate_limited(f"create-ip:{client_ip()}", 5)
                or rate_limited(f"create-email:{values['organizer_email']}", 5)):
            delete_image(values["image"])
            abort(429)

        db = get_db()
        code = new_code()
        with db:
            cur = db.execute(
                """INSERT INTO events (slug, name, description, image, starts_at, ends_at, location,
                       organizer_name, organizer_email, admin_code_hash, listed,
                       registration_opens_at, registration_closes_at, max_per_person, created_at)
                   VALUES (:slug, :name, :description, :image, :starts_at, :ends_at, :location,
                       :organizer_name, :organizer_email, :admin_code_hash, :listed,
                       :registration_opens_at, :registration_closes_at, :max_per_person, :created_at)""",
                {**values, "slug": make_slug(values["name"]), "admin_code_hash": hash_code(code),
                 "created_at": now_db()},
            )
        ok = mail.send(values["organizer_email"], f"Staðfestu viðburð: {values['name']}",
                       "event_created.txt", values=values, code=code,
                       link=external_url("access", code=code))
        if not ok:
            with db:
                db.execute("DELETE FROM events WHERE id = ?", (cur.lastrowid,))
            delete_image(values["image"])
            errors["form"] = "Ekki tókst að senda tölvupóst. Reyndu aftur síðar."
            return render_template("event_form.html", creating=True, need_password=need_password,
                                   values=request.form, errors=errors), 500
        return redirect(url_for("sent", to="event"))

    def load_admin_event(slug):
        event = get_db().execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
        if event is None:
            abort(404)
        if not has_access("events", event["id"]):
            abort(403)
        return event

    def load_admin_queue(event, queue_id):
        queue = get_db().execute(
            "SELECT * FROM queues WHERE id = ? AND event_id = ?", (queue_id, event["id"])
        ).fetchone()
        if queue is None:
            abort(404)
        return queue

    @app.route("/admin/<slug>")
    def admin(slug):
        event = load_admin_event(slug)
        db = get_db()
        queues = []
        for q in db.execute(
            "SELECT * FROM queues WHERE event_id = ? ORDER BY starts_at, ends_at, sort_order, id",
            (event["id"],),
        ).fetchall():
            (pending,) = db.execute(
                """SELECT COUNT(*) FROM registrations
                   WHERE queue_id = ? AND verified_at IS NULL AND cancelled_at IS NULL""",
                (q["id"],),
            ).fetchone()
            queues.append({"queue": q, "standing": queue_standing(db, q), "pending": pending,
                           "link": external_url("game_page", slug=slug, queue_id=q["id"])})
        return render_template("admin.html", event=event, queues=queues,
                               public_url=external_url("event_page", slug=slug))

    @app.route("/admin/<slug>/edit", methods=["GET", "POST"])
    def edit_event(slug):
        event = load_admin_event(slug)
        if request.method == "GET":
            return render_template("event_form.html", creating=False, event=event,
                                   values=event_form_values(event), errors={})
        values, errors = parse_event_form(request.form, creating=False)
        if not errors:
            outside = get_db().execute(
                "SELECT name FROM queues WHERE event_id = ? AND (starts_at < ? OR ends_at > ?)",
                (event["id"], values["starts_at"], values["ends_at"]),
            ).fetchall()
            if outside:
                errors["end"] = ("Þessi spil yrðu utan nýja tímans; breyttu þeim fyrst: "
                                 + ", ".join(f"„{q['name']}“" for q in outside))
        new_image = None
        if not errors:
            try:
                new_image = save_image(request.files.get("image"))
            except ValueError as e:
                errors["image"] = str(e)
        if errors:
            return render_template("event_form.html", creating=False, event=event,
                                   values={**event_form_values(event), **request.form,
                                           "listed": request.form.get("listed", "")},
                                   errors=errors), 400

        image = replace_image(event["image"], new_image)
        db = get_db()
        with db:
            db.execute(
                """UPDATE events SET name = :name, description = :description, image = :image,
                       starts_at = :starts_at, ends_at = :ends_at, location = :location,
                       listed = :listed,
                       registration_opens_at = :registration_opens_at,
                       registration_closes_at = :registration_closes_at,
                       max_per_person = :max_per_person
                   WHERE id = :id""",
                {**values, "image": image, "id": event["id"]},
            )
        flash("Breytingar vistaðar.")
        return redirect(url_for("admin", slug=slug))

    @app.route("/admin/<slug>/cancel", methods=["POST"])
    def cancel_event(slug):
        event = load_admin_event(slug)
        if not request.form.get("confirm"):
            flash("Hakaðu í reitinn til að staðfesta að aflýsa eigi viðburðinum.")
            return redirect(url_for("admin", slug=slug))
        if event["status"] == "cancelled":
            return redirect(url_for("admin", slug=slug))
        db = get_db()
        with db:
            db.execute("UPDATE events SET status = 'cancelled' WHERE id = ?", (event["id"],))
        message = request.form.get("message", "").strip()[:2000]
        for reg in db.execute(
            """SELECT r.*, q.name AS queue_name FROM registrations r JOIN queues q ON q.id = r.queue_id
               WHERE r.event_id = ? AND r.verified_at IS NOT NULL AND r.cancelled_at IS NULL""",
            (event["id"],),
        ).fetchall():
            mail.send(reg["email"], f"Viðburði aflýst: {event['name']}", "event_cancelled.txt",
                      reg=reg, event=event, message=message)
        for queue in db.execute(
            "SELECT * FROM queues WHERE event_id = ? AND runner_email IS NOT NULL", (event["id"],)
        ).fetchall():
            mail.send(queue["runner_email"], f"Viðburði aflýst: {event['name']}", "event_cancelled.txt",
                      reg={"name": queue["runner_name"] or "", "queue_name": queue["name"]},
                      event=event, message=message, runner=True)
        flash("Viðburðinum hefur verið aflýst og þátttakendum sendur tölvupóstur.")
        return redirect(url_for("admin", slug=slug))

    @app.route("/admin/<slug>/queues/new", methods=["GET", "POST"])
    @app.route("/admin/<slug>/queues/<int:queue_id>/edit", methods=["GET", "POST"])
    def edit_queue(slug, queue_id=None):
        event = load_admin_event(slug)
        db = get_db()
        queue = load_admin_queue(event, queue_id) if queue_id else None
        if request.method == "GET":
            if queue:
                values = queue_form_values(queue)
            else:
                # Default to the time of the most recently added game, else the whole event
                last = db.execute(
                    "SELECT starts_at, ends_at FROM queues WHERE event_id = ? ORDER BY id DESC LIMIT 1",
                    (event["id"],),
                ).fetchone() or event
                (next_order,) = db.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM queues WHERE event_id = ?",
                    (event["id"],),
                ).fetchone()
                values = {"is_open": "1", "sort_order": next_order,
                          "date": local_input(last["starts_at"], "%Y-%m-%d"),
                          "start_time": local_input(last["starts_at"], "%H:%M"),
                          "end_time": local_input(last["ends_at"], "%H:%M")}
            return render_template("queue_form.html", event=event, queue=queue, values=values,
                                   slots=time_slots(event), errors={})

        values, errors = parse_queue_form(request.form, event)
        new_image = None
        if not errors:
            try:
                new_image = save_image(request.files.get("image"))
            except ValueError as e:
                errors["image"] = str(e)
        if errors:
            return render_template("queue_form.html", event=event, queue=queue, values=request.form,
                                   slots=time_slots(event), errors=errors), 400

        if queue is None:
            with db:
                queue_id = db.execute(
                    """INSERT INTO queues (event_id, name, description, image, capacity, starts_at,
                           ends_at, sort_order, is_open, runner_name, runner_email)
                       VALUES (:event_id, :name, :description, :image, :capacity, :starts_at,
                           :ends_at, :sort_order, :is_open, :runner_name, :runner_email)""",
                    {**values, "event_id": event["id"], "image": new_image},
                ).lastrowid
            flash(f"Spilið „{values['name']}“ var stofnað.")
            if values["runner_email"]:
                flash_runner_invite(event, load_admin_queue(event, queue_id))
            return redirect(url_for("admin", slug=slug))

        image = replace_image(queue["image"], new_image)
        with db:
            db.execute(
                """UPDATE queues SET name = :name, description = :description, image = :image,
                       capacity = :capacity, starts_at = :starts_at, ends_at = :ends_at,
                       sort_order = :sort_order, is_open = :is_open,
                       runner_name = :runner_name, runner_email = :runner_email
                   WHERE id = :id""",
                {**values, "image": image, "id": queue["id"]},
            )
            if values["runner_email"] is None:
                # No runner any more: the old runner's code and links stop working
                db.execute("UPDATE queues SET runner_code_hash = NULL WHERE id = ?", (queue["id"],))
            if values["starts_at"] != queue["starts_at"]:
                db.execute("UPDATE registrations SET reminder_sent_at = NULL WHERE queue_id = ?",
                           (queue["id"],))
        updated = load_admin_queue(event, queue["id"])
        changed = sync_queue_states(db, queue["id"])
        notify_state_changes(changed, event, updated)
        flash("Breytingar vistaðar." + (f" {len(changed)} þátttakendum var tilkynnt um breytta stöðu."
                                        if changed else ""))
        if values["runner_email"] and values["runner_email"] != queue["runner_email"]:
            flash_runner_invite(event, updated)
        if (values["starts_at"], values["ends_at"]) != (queue["starts_at"], queue["ends_at"]):
            # Moving a game can make existing registrations overlap; tell the organizer.
            clashes = [
                r["name"]
                for r in db.execute(
                    "SELECT * FROM registrations WHERE queue_id = ? AND cancelled_at IS NULL", (queue["id"],)
                ).fetchall()
                if overlapping_registration(db, event["id"], r["email"], values["starts_at"],
                                            values["ends_at"], exclude_queue_id=queue["id"])
            ]
            if clashes:
                flash("Athugið: eftir tímabreytinguna eru þessi skráð í annað spil á sama tíma: "
                      + ", ".join(clashes))
        return redirect(url_for("admin", slug=slug))

    def flash_runner_invite(event, queue):
        if invite_runner(event, queue):
            flash(f"Boð með kóða var sent á {queue['runner_email']}.")
        else:
            flash(f"Ekki tókst að senda boð á {queue['runner_email']}. Reyndu „Senda boð aftur“.")

    @app.route("/admin/<slug>/queues/<int:queue_id>/invite", methods=["POST"])
    def reinvite_runner(slug, queue_id):
        event = load_admin_event(slug)
        queue = load_admin_queue(event, queue_id)
        if queue["runner_email"]:
            flash_runner_invite(event, queue)
        return redirect(url_for("admin", slug=slug))

    @app.route("/admin/<slug>/queues/<int:queue_id>/delete", methods=["POST"])
    def delete_queue(slug, queue_id):
        event = load_admin_event(slug)
        queue = load_admin_queue(event, queue_id)
        db = get_db()
        (active,) = db.execute(
            "SELECT COUNT(*) FROM registrations WHERE queue_id = ? AND cancelled_at IS NULL",
            (queue_id,),
        ).fetchone()
        if active:
            flash("Ekki er hægt að eyða spili sem hefur skráningar. Lokaðu því frekar eða fjarlægðu þátttakendur fyrst.")
        else:
            with db:
                db.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
            delete_image(queue["image"])
            flash(f"Spilinu „{queue['name']}“ var eytt.")
        return redirect(url_for("admin", slug=slug))

    @app.route("/admin/<slug>/registrations/<int:reg_id>/remove", methods=["POST"])
    def remove_registration(slug, reg_id):
        event = load_admin_event(slug)
        db = get_db()
        reg = db.execute(
            "SELECT * FROM registrations WHERE id = ? AND event_id = ?", (reg_id, event["id"])
        ).fetchone()
        if reg is None:
            abort(404)
        if reg["cancelled_at"] is None:
            queue = load_admin_queue(event, reg["queue_id"])
            was_verified = reg["verified_at"] is not None
            cancel_registration(reg, event, queue)
            if was_verified:
                mail.send(reg["email"], f"Skráning felld niður: {event['name']}", "removed.txt",
                          reg=reg, event=event, queue=queue)
            flash(f"{reg['name']} var fjarlægð/ur úr „{queue['name']}“.")
        return redirect(url_for("admin", slug=slug))

    @app.route("/admin/<slug>/registrations/<int:reg_id>/paid", methods=["POST"])
    def toggle_paid(slug, reg_id):
        event = load_admin_event(slug)
        db = get_db()
        reg = db.execute(
            "SELECT * FROM registrations WHERE id = ? AND event_id = ?", (reg_id, event["id"])
        ).fetchone()
        if reg is None:
            abort(404)
        paid_at = None if reg["paid_at"] else now_db()
        with db:
            db.execute("UPDATE registrations SET paid_at = ? WHERE id = ?", (paid_at, reg_id))
        flash(f"Greiðsla {reg['name']} merkt staðfest." if paid_at else f"Merking um greiðslu {reg['name']} fjarlægð.")
        return redirect(url_for("admin", slug=slug) + f"#q{reg['queue_id']}")

    @app.route("/admin/<slug>/export.csv")
    def export_csv(slug):
        event = load_admin_event(slug)
        db = get_db()
        out = io.StringIO()
        out.write("﻿")  # BOM so Excel detects UTF-8
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Spil", "Tími", "Nafn", "Netfang", "Staða", "Sæti", "Greitt", "Skráð", "Staðfest", "Afskráð"])
        for q in db.execute(
            "SELECT * FROM queues WHERE event_id = ? ORDER BY starts_at, ends_at, sort_order, id",
            (event["id"],),
        ).fetchall():
            for reg in db.execute(
                "SELECT * FROM registrations WHERE queue_id = ? ORDER BY verified_at IS NULL, verified_at, id",
                (q["id"],),
            ).fetchall():
                st = registration_state(db, reg)
                writer.writerow([
                    q["name"], fmt_range(q["starts_at"], q["ends_at"]), reg["name"], reg["email"],
                    STATE_LABELS[st["state"]],
                    st["position"] or "", "já" if reg["paid_at"] else "", fmt_dt(reg["created_at"], with_day=False),
                    fmt_dt(reg["verified_at"], with_day=False), fmt_dt(reg["cancelled_at"], with_day=False),
                ])
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{event["slug"]}.csv"'},
        )

    return app
