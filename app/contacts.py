"""Contact validation. Mirrors the database's checks (migration 003) so staff get
a clear message instead of a database error, and junk like 'redacted' never gets in."""
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

REASON_LABELS = {
    "no_contact": "No parent contact on file",
    "no_email": "Phone only, no email",
    "notices_off": "Parent opted out of emails",
}


def clean_email(raw):
    """Return (email_or_None, error_or_None). Blank is allowed (means 'no email')."""
    value = (raw or "").strip()
    if not value:
        return None, None
    if not EMAIL_RE.match(value):
        return None, f"“{value}” doesn't look like an email address."
    return value.lower(), None


def clean_phone(raw):
    value = (raw or "").strip()
    if not value:
        return None, None
    digits = re.sub(r"\D", "", value)
    if not 10 <= len(digits) <= 15:
        return None, f"“{value}” doesn't look like a phone number (needs 10–15 digits)."
    return value, None


def clean_name(raw):
    value = (raw or "").strip()
    return value or None


def clean_contact_form(form):
    """Validate a guardian form. Returns (values, errors)."""
    email, email_err = clean_email(form.get("email"))
    phone, phone_err = clean_phone(form.get("phone"))
    errors = [e for e in (email_err, phone_err) if e]
    if not errors and email is None and phone is None:
        errors.append("Enter an email or a phone number.")
    return {"name": clean_name(form.get("name")), "email": email, "phone": phone}, errors


def csv_safe(value):
    """Stop spreadsheet formula injection when a cell is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
