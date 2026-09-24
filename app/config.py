"""Configuration from environment variables. Fails loudly on missing production settings."""
import os
from datetime import timedelta


class ConfigError(RuntimeError):
    pass


def load_config(overrides=None):
    env = os.environ.get("APP_ENV", "production").lower()
    cfg = {
        "APP_ENV": env,
        "SECRET_KEY": os.environ.get("SECRET_KEY"),
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "SUPABASE_URL": (os.environ.get("SUPABASE_URL") or "").rstrip("/"),
        "SUPABASE_ANON_KEY": os.environ.get("SUPABASE_ANON_KEY"),
        "INSTITUTION_SLUG": os.environ.get("INSTITUTION_SLUG", "sacred-heart"),
        # Email. outbox = nothing is emailed, messages are kept for review in the app.
        #        test   = everything goes to EMAIL_TEST_RECIPIENT, never to parents.
        #        live   = real parents. Must be chosen explicitly.
        "EMAIL_MODE": (os.environ.get("EMAIL_MODE") or "outbox").strip().lower(),
        "EMAIL_TEST_RECIPIENT": (os.environ.get("EMAIL_TEST_RECIPIENT") or "").strip(),
        "SENDGRID_API_KEY": os.environ.get("SENDGRID_API_KEY") or "",
        "EMAIL_FROM": (os.environ.get("EMAIL_FROM") or "").strip(),
        "EMAIL_FROM_NAME": (os.environ.get("EMAIL_FROM_NAME") or "MealMode").strip(),
        "SUPPORT_EMAIL": (os.environ.get("SUPPORT_EMAIL") or "").strip(),
        # Where parents reach the app, e.g. https://billing.mealmode.com (used in email links).
        "PUBLIC_BASE_URL": (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/"),
        # Secret for parent portal links. Changing it breaks every link already emailed.
        "PORTAL_SECRET": os.environ.get("PORTAL_SECRET") or "",
        # Cookies
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": env != "development",
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=12),
    }
    if overrides:
        cfg.update(overrides)
    if not cfg["PUBLIC_BASE_URL"] and cfg["APP_ENV"] == "development":
        cfg["PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"

    check_email_config(cfg)
    if cfg.get("TESTING"):
        return cfg

    missing = [k for k in ("SECRET_KEY", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY") if not cfg.get(k)]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    if cfg["APP_ENV"] != "development" and len(cfg["SECRET_KEY"]) < 32:
        raise ConfigError("SECRET_KEY must be at least 32 characters outside development")
    if len(cfg["PORTAL_SECRET"]) < 32:
        raise ConfigError("PORTAL_SECRET must be set (32+ random characters). It signs the parent portal links.")
    if not cfg["PUBLIC_BASE_URL"].startswith(("https://", "http://")):
        raise ConfigError("PUBLIC_BASE_URL must be set, e.g. https://your-app.up.railway.app (used in email links)")
    return cfg


def check_email_config(cfg):
    mode = cfg["EMAIL_MODE"]
    if mode not in ("outbox", "test", "live"):
        raise ConfigError("EMAIL_MODE must be outbox, test, or live")
    if mode in ("test", "live"):
        missing = [k for k in ("SENDGRID_API_KEY", "EMAIL_FROM") if not cfg.get(k)]
        if missing:
            raise ConfigError(f"EMAIL_MODE={mode} needs " + ", ".join(missing))
    if mode == "test" and "@" not in cfg["EMAIL_TEST_RECIPIENT"]:
        raise ConfigError("EMAIL_MODE=test needs EMAIL_TEST_RECIPIENT: the one inbox that receives every email")
