"""MealMode postpaid billing — Flask application factory."""
from flask import Flask, g, render_template, request

from .auth import SupabaseAuth, check_csrf, csrf_token, load_current_staff
from .config import load_config
from .db import Database
from .repo import Repo, role_at_least


def create_app(overrides=None, repo=None, auth=None):
    app = Flask(__name__)
    app.config.update(load_config(overrides))

    if repo is None:
        repo = Repo(Database(app.config["DATABASE_URL"]))
    if auth is None:
        auth = SupabaseAuth(app.config["SUPABASE_URL"], app.config["SUPABASE_ANON_KEY"])
    app.extensions["repo"] = repo
    app.extensions["auth"] = auth

    @app.before_request
    def _before():
        g.staff, g.institution = None, None
        if request.endpoint == "static":
            return
        load_current_staff()
        check_csrf()
        g.institution = repo.get_institution(app.config["INSTITUTION_SLUG"])

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["role_at_least"] = role_at_least
    app.jinja_env.filters["dollars"] = format_cents
    app.jinja_env.filters["when"] = format_when

    for code in (400, 403, 404, 500):
        app.register_error_handler(code, _error_page(code))

    from .views import bp
    app.register_blueprint(bp)

    from .cli import register_cli
    register_cli(app)
    return app


def format_cents(cents):
    if cents is None:
        return "—"
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def format_when(value, tz="America/New_York"):
    """Timestamps (datetime from psycopg, text from psql) -> 'Sep 21, 2026 2:05 AM' in school time."""
    if value in (None, ""):
        return "—"
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    if isinstance(value, str):
        try:
            value = (date.fromisoformat(value) if len(value) == 10
                     else datetime.fromisoformat(value.replace(" ", "T", 1)))
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(ZoneInfo(tz))
        hour = value.hour % 12 or 12
        return f"{value:%b} {value.day}, {value.year} {hour}:{value:%M} {'AM' if value.hour < 12 else 'PM'}"
    return f"{value:%b} {value.day}, {value.year}"


def _error_page(code):
    titles = {400: "Bad request", 403: "No access", 404: "Not found", 500: "Something went wrong"}

    def handler(err):
        description = getattr(err, "description", None) if code != 500 else None
        return render_template("error.html", code=code, title=titles[code], description=description), code
    return handler
