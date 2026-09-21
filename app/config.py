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
        # Cookies
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": env != "development",
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=12),
    }
    if overrides:
        cfg.update(overrides)

    if cfg.get("TESTING"):
        return cfg

    missing = [k for k in ("SECRET_KEY", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY") if not cfg.get(k)]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    if cfg["APP_ENV"] != "development" and len(cfg["SECRET_KEY"]) < 32:
        raise ConfigError("SECRET_KEY must be at least 32 characters outside development")
    return cfg
