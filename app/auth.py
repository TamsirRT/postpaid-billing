"""Staff authentication via Supabase Auth (email + password), plus access control.

* Supabase verifies the password; we never see or store it.
* After a successful sign-in, the Flask session holds only the user's id and email.
* The role is re-read from billing.staff_roles on EVERY request, so revoking
  access takes effect immediately, not when the session expires.
* A new sign-in has no role and sees only the "pending access" page until a
  super admin grants one.
"""
import hmac
import secrets
from functools import wraps

import requests
from flask import abort, current_app, g, redirect, request, session, url_for

from .repo import role_at_least


class AuthError(Exception):
    """Sign-in or sign-up failed. The message is safe to show the user."""


class SupabaseAuth:
    def __init__(self, url, anon_key, timeout=10):
        self.url = url
        self.anon_key = anon_key
        self.timeout = timeout

    def _post(self, path, payload):
        try:
            resp = requests.post(
                f"{self.url}/auth/v1/{path}",
                json=payload,
                headers={"apikey": self.anon_key, "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise AuthError("Couldn't reach the sign-in service. Try again in a minute.")
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        return resp.status_code, body

    def sign_in(self, email, password):
        status, body = self._post("token?grant_type=password", {"email": email, "password": password})
        if status == 200 and body.get("user", {}).get("id"):
            user = body["user"]
            return {"user_id": user["id"], "email": user.get("email") or email}
        if status in (400, 401):
            # Supabase uses 400 for bad credentials and for unconfirmed emails.
            msg = (body.get("error_description") or body.get("msg") or "").lower()
            if "confirm" in msg:
                raise AuthError("Confirm your email address first, using the link Supabase sent you.")
            raise AuthError("Email or password is incorrect.")
        if status == 429:
            raise AuthError("Too many attempts. Wait a few minutes and try again.")
        raise AuthError("Sign-in failed. Try again in a minute.")

    def sign_up(self, email, password):
        status, body = self._post("signup", {"email": email, "password": password})
        if status in (200, 201):
            return True
        msg = body.get("msg") or body.get("error_description") or ""
        if status == 422 and msg:
            raise AuthError(msg)
        if status == 429:
            raise AuthError("Too many attempts. Wait a few minutes and try again.")
        raise AuthError("Couldn't create the account. Try again, or ask a super admin.")


# ----------------------------------------------------------------- sessions

def log_in(user_id, email):
    session.clear()
    session.permanent = True
    session["uid"] = user_id
    session["email"] = email
    session["csrf"] = secrets.token_urlsafe(32)


def log_out():
    session.clear()


def load_current_staff():
    """before_request: put the signed-in staff member (with a fresh role) on g."""
    g.staff = None
    uid = session.get("uid")
    if uid:
        g.staff = current_app.extensions["repo"].get_staff(uid)
        if g.staff is None:
            # Row vanished (manual DB edit). Treat as signed out.
            session.clear()


def require_role(needed):
    """Decorator: signed in AND holding at least `needed` role."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.get("staff"):
                return redirect(url_for("main.login", next=request.path))
            if g.staff.get("role") is None:
                return redirect(url_for("main.pending"))
            if not role_at_least(g.staff.get("role"), needed):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# --------------------------------------------------------------------- CSRF

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    """before_request: every state-changing request must echo the session's token."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf") or ""
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, description="Your form expired. Go back, refresh the page, and try again.")
